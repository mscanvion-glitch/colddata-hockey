import os
import json
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

CORRIDOR_MIN = 1.25
CORRIDOR_MAX = 1.55
SEUIL_TIER_1 = 85.0
SEUIL_TIER_2 = 82.0
SEUIL_TIER_3 = 80.0

def send_telegram_alert(message):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        print(f"Erreur Telegram: {e}")

def get_real_starting_goalies():
    url = "https://www.dailyfaceoff.com/starting-goalies"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    goalies = {}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            cards = soup.find_all("div", class_=lambda x: x and "goalie-card" in x)
            for card in cards:
                try:
                    team_el = card.find("span", class_=lambda x: x and "team" in x)
                    status_el = card.find("span", class_=lambda x: x and "status" in x)
                    name_el = card.find("h4")
                    if team_el and name_el:
                        team_key = team_el.text.strip().lower()
                        status_text = status_el.text.strip().lower() if status_el else "projected"
                        is_confirmed = "confirmed" in status_text
                        goalies[team_key] = {
                            "name": name_el.text.strip(),
                            "confirmed": is_confirmed
                        }
                except Exception:
                    continue
    except Exception as e:
        print(f"Scraping gardiens warning: {e}")
    return goalies

def get_last_5_games_record(team_abbr):
    url = f"https://api-web.nhle.com/v1/club-schedule-season/{team_abbr}/now"
    pts, goals_for, goals_against = 0, 0, 0
    count = 0
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            past_games = [g for g in r.json().get("games", []) if g.get("gameState") in ["OFF", "FINAL"]]
            for g in past_games[-5:]:
                count += 1
                is_home = g["homeTeam"]["abbrev"] == team_abbr
                s_team = g["homeTeam"]["score"] if is_home else g["awayTeam"]["score"]
                s_opp = g["awayTeam"]["score"] if is_home else g["homeTeam"]["score"]
                goals_for += s_team
                goals_against += s_opp
                if s_team > s_opp: pts += 2
                elif g.get("gameOutcome", {}).get("lastPeriodType") in ["OT", "SO"]: pts += 1
    except Exception:
        pass
    return {"points": pts, "diff": goals_for - goals_against, "count": count}

def check_back_to_back(team_abbr):
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://api-web.nhle.com/v1/schedule/{yesterday}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            for g in r.json().get("gameWeek", [{}])[0].get("games", []):
                if g["homeTeam"]["abbrev"] == team_abbr or g["awayTeam"]["abbrev"] == team_abbr:
                    return True
    except Exception:
        pass
    return False

def fetch_live_odds():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key: return {}
    url = f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/?apiKey={api_key}&regions=eu&markets=h2h"
    odds_map = {}
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            for item in r.json():
                h = item.get("home_team")
                bookmakers = item.get("bookmakers", [])
                if bookmakers:
                    markets = bookmakers[0].get("markets", [])
                    if markets:
                        for outcome in markets[0].get("outcomes", []):
                            if outcome["name"] == h:
                                odds_map[h] = outcome["price"]
    except Exception:
        pass
    return odds_map

def run_pipeline():
    today = datetime.now().strftime("%Y-%m-%d")
    schedule_url = f"https://api-web.nhle.com/v1/schedule/{today}"
    try:
        res = requests.get(schedule_url, timeout=10)
        games = res.json().get("gameWeek", [{}])[0].get("games", [])
    except Exception:
        games = []

    odds_data = fetch_live_odds()
    goalies_data = get_real_starting_goalies()
    results = []

    for g in games:
        home_name = g["homeTeam"]["commonName"]["default"]
        away_name = g["awayTeam"]["commonName"]["default"]
        home_abbr = g["homeTeam"]["abbrev"]
        away_abbr = g["awayTeam"]["abbrev"]
        time_utc = g.get("startTimeUTC", "").split("T")[-1][:5] + " UTC"
        
        reasons = []
        
        # 1. Cote réelle obligatoire
        real_odds = odds_data.get(home_name)
        if real_odds is None:
            reasons.append("Cote réelle absente (Vérifier ODDS_API_KEY)")
            real_odds = 0.0
        elif real_odds < CORRIDOR_MIN or real_odds > CORRIDOR_MAX:
            reasons.append(f"Cote hors corridor [{CORRIDOR_MIN}-{CORRIDOR_MAX}] (@{real_odds})")

        # 2. Gardien partant
        goalie_info = goalies_data.get(home_abbr.lower(), {"name": "Non communiqué", "confirmed": False})
        if not goalie_info["confirmed"]:
            reasons.append(f"Gardien non confirmé ({goalie_info['name']})")

        # 3. Fatigue B2B
        if check_back_to_back(home_abbr):
            reasons.append("Équipe domicile en Back-to-Back")

        # 4. Calcul clinique
        home_form = get_last_5_games_record(home_abbr)
        away_form = get_last_5_games_record(away_abbr)
        
        score_forme = (home_form["points"] / 10.0) * 30.0 if home_form["count"] >= 3 else 15.0
        score_diff = min(25.0, max(5.0, 15.0 + ((home_form["diff"] - away_form["diff"]) * 1.2)))
        score_repos = 25.0 if not check_back_to_back(away_abbr) else 15.0
        score_terrain = 20.0
        
        total_score = score_forme + score_diff + score_repos + score_terrain

        if reasons:
            results.append({
                "match": f"{home_name} vs {away_name}",
                "league": "NHL",
                "time": time_utc,
                "cote": real_odds,
                "score": round(total_score, 1),
                "status": "REJETÉ",
                "motifs": reasons,
                "stake_pct": 0,
                "gardien": goalie_info["name"]
            })
        else:
            if total_score >= SEUIL_TIER_1:
                status = "PÉPITE VALIDÉE (TIER 1)"
                stake_pct = 15 if real_odds > 1.40 else 20
            elif total_score >= SEUIL_TIER_2:
                status = "VALUE SECONDAIRE (TIER 2)"
                stake_pct = 10
            elif total_score >= SEUIL_TIER_3:
                status = "SPOT MODÉRÉ (TIER 3)"
                stake_pct = 5
            else:
                status = "REJETÉ"
                stake_pct = 0
                reasons.append(f"Score insuffisant ({total_score:.1f}/100)")

            item = {
                "match": f"{home_name} vs {away_name}",
                "league": "NHL",
                "time": time_utc,
                "cote": real_odds,
                "score": round(total_score, 1),
                "status": status,
                "motifs": reasons,
                "stake_pct": stake_pct,
                "gardien": goalie_info["name"]
            }
            results.append(item)
            send_telegram_alert(f"🚨 **COLD_DATA HOCKEY**\n\nMatch : {item['match']}\nCote : @{real_odds}\nScore : {item['score']}/100\nMise : {stake_pct}%")

    output = {
        "updated_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "total_scanned": len(results),
        "results": results
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    run_pipeline()
