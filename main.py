import os
import logging
from datetime import datetime
import pytz
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import swisseph as swe
import google.generativeai as genai
import requests
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Load environment variables for LINE Bot and Google Geocoding API
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
GOOGLE_GEOCODING_API_KEY = os.getenv('GOOGLE_GEOCODING_API_KEY')

# --- Gemini API Key (Hardcoded as a last resort due to IAM issues) ---
# NOTE: For production environments, it is highly recommended to use Google Cloud Secret Manager
# to securely store and retrieve API keys, rather than hardcoding them.
# This is a temporary workaround to get the functionality working given the persistent IAM difficulties.
GEMINI_API_KEY = "AIzaSyANkrDLX2wSdva0m153up5O-h0VLOWq30o"
# --- End Gemini API Key ---

if not LINE_CHANNEL_SECRET:
    logger.error("LINE_CHANNEL_SECRET environment variable is not set.")
    raise ValueError("LINE_CHANNEL_SECRET environment variable is not set.")
if not LINE_CHANNEL_ACCESS_TOKEN:
    logger.error("LINE_CHANNEL_ACCESS_TOKEN environment variable is not set.")
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN environment variable is not set.")
if not GOOGLE_GEOCODING_API_KEY:
    logger.error("GOOGLE_GEOCODING_API_KEY environment variable is not set.")
    raise ValueError("GOOGLE_GEOCODING_API_KEY environment variable is not set.")
if not GEMINI_API_KEY:
    logger.error("GEMINI_API_KEY is not set. Please check the hardcoded value or environment variable.")
    # If GEMINI_API_KEY is still not set, the app will not function correctly.
    # We will proceed, but expect Gemini API calls to fail.

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Configure Gemini API
# If GEMINI_API_KEY is loaded, configure it. Otherwise, Gemini API calls will fail.
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    logger.warning("Gemini API not configured due to missing API key.")

# Set the path to the ephemeris data files
# This path should match where the 'ephe' directory is copied within the Cloud Run container
swe.set_ephe_path(os.path.join(os.path.dirname(__file__), 'ephe'))
logger.info(f"Swiss Ephemeris path set to: {swe.get_ephe_path()}")

def get_coordinates(city, region):
    """
    Uses Google Geocoding API to get latitude and longitude for a given city and region.
    """
    if not GOOGLE_GEOCODING_API_KEY:
        logger.error("Google Geocoding API key is missing.")
        return None, None

    address = f"{city}, {region}, Japan" # Assuming Japan for now
    geocode_url = f"https://maps.googleapis.com/maps/api/geocode/json?address={address}&key={GOOGLE_GEOCODING_API_KEY}"
    
    try:
        response = requests.get(geocode_url)
        response.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)
        data = response.json()

        if data['status'] == 'OK' and data['results']:
            location = data['results'][0]['geometry']['location']
            lat = location['lat']
            lon = location['lng']
            logger.info(f"Geocoding successful. Lat: {lat}, Lon: {lon}")
            return lat, lon
        else:
            logger.error(f"Geocoding API response status: {data['status']}. Error: {data.get('error_message', 'No error message provided.')}")
            return None, None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error calling Geocoding API: {e}")
        return None, None
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding Geocoding API response: {e}")
        return None, None

def calculate_natal_chart(birth_date_str, birth_time_str, latitude, longitude):
    """
    Calculates the natal chart positions of planets using Swiss Ephemeris.
    """
    try:
        # Parse birth date and time
        birth_datetime_str = f"{birth_date_str} {birth_time_str}"
        birth_datetime = datetime.strptime(birth_datetime_str, "%Y-%m-%d %H:%M")
        
        # Assume UTC offset for Japan (JST is UTC+9)
        # For precise calculation, timezone lookup for the specific location is ideal,
        # but for simplicity, we'll use a fixed offset or rely on swe.utc_time_zone.
        # swe.utc_time_zone is not directly available for conversion in this context,
        # so we'll use a fixed offset for Japan.
        
        # Get UTC time for Swiss Ephemeris
        # Using a fixed offset for JST (UTC+9)
        tz = pytz.timezone('Asia/Tokyo')
        local_dt = tz.localize(birth_datetime)
        utc_dt = local_dt.astimezone(pytz.utc)

        jd_utc = swe.utc_to_jd(utc_dt.year, utc_dt.month, utc_dt.day,
                               utc_dt.hour, utc_dt.minute, utc_dt.second, swe.FLG_SWIEPH)

        # Calculate planet positions
        planets = {
            "Sun": swe.SUN, "Moon": swe.MOON, "Mercury": swe.MERCURY,
            "Venus": swe.VENUS, "Mars": swe.MARS, "Jupiter": swe.JUPITER,
            "Saturn": swe.SATURN, "Uranus": swe.URANUS, "Neptune": swe.NEPTUNE,
            "Pluto": swe.PLUTO
        }
        
        natal_positions = {}
        for name, p_id in planets.items():
            xx, ret = swe.calc_ut(jd_utc[0], p_id, swe.FLG_SWIEPH)
            natal_positions[name] = (xx[0], xx[1]) # Longitude, speed

        # Calculate houses and ASC/MC
        cusps, ascmc, ret = swe.houses(jd_utc[0], latitude, longitude, b'P') # 'P' for Placidus house system
        natal_positions["ASC"] = (ascmc[0],) # Ascendant longitude
        natal_positions["MC"] = (ascmc[1],) # Midheaven longitude

        logger.info("Horoscope calculation completed.")
        return natal_positions

    except Exception as e:
        logger.error(f"Error during natal chart calculation: {e}")
        return None

def get_zodiac_sign(longitude):
    """
    Determines the zodiac sign from a given longitude.
    """
    zodiac_signs = [
        "牡羊座", "牡牛座", "双子座", "蟹座", "獅子座", "乙女座",
        "天秤座", "蠍座", "射手座", "山羊座", "水瓶座", "魚座"
    ]
    # Each sign is 30 degrees.
    # Aries starts at 0 degrees.
    sign_index = int(longitude / 30) % 12
    return zodiac_signs[sign_index]

def generate_horoscope_interpretation(natal_positions):
    """
    Uses Gemini API to generate a horoscope interpretation based on natal positions.
    """
    if not GEMINI_API_KEY:
        logger.error("Gemini API key is not configured. Cannot generate interpretation.")
        return "ホロスコープの解釈中にエラーが発生しました。APIキーが設定されていません。"

    try:
        # Construct a detailed prompt for Gemini
        prompt_parts = [
            "あなたは経験豊富な占星術師です。以下の天体位置とハウスカスプ情報に基づいて、"
            "その人の性格や傾向について、詳細かつ洞察に満ちたホロスコープの解釈を生成してください。\n\n",
            "**天体位置:**\n"
        ]
        for name, pos in natal_positions.items():
            if name in ["ASC", "MC"]:
                prompt_parts.append(f"- {name}: {get_zodiac_sign(pos[0])} ({pos[0]:.2f}度)\n")
            else:
                prompt_parts.append(f"- {name}: {get_zodiac_sign(pos[0])} ({pos[0]:.2f}度)\n")
        
        prompt_parts.append("\n解釈は、各天体の位置が示す意味を簡潔にまとめ、全体的な性格傾向を説明してください。")

        logger.info(f"LLM Prompt: {''.join(prompt_parts)}")

        model = genai.GenerativeModel(model_name="gemini-2.0-flash")
        
        # Use a timeout for the API call
        response = model.generate_content(prompt_parts, request_options={"timeout": 60})
        
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            interpretation_text = response.candidates[0].content.parts[0].text
            logger.info("Gemini API interpretation generated successfully.")
            return interpretation_text
        else:
            logger.error("LLM API returned no text or unexpected structure.")
            return "ホロスコープの解釈中にエラーが発生しました。AIからの応答がありませんでした。"

    except Exception as e:
        logger.error(f"Gemini API request failed: {e}")
        # Check for specific error types if possible
        if "403 Client Error" in str(e) or "Forbidden" in str(e):
            return "ホロスコープの解釈中にネットワークエラーが発生しました。権限の問題の可能性があります。時間を置いて再度お試しください。"
        elif "Quota exceeded" in str(e) or "rate limit" in str(e):
            return "ホロスコープの解釈中にネットワークエラーが発生しました。APIの利用制限を超過しました。時間を置いて再度お試しください。"
        elif "timeout" in str(e):
            return "ホロスコープの解釈中にネットワークエラーが発生しました。AIからの応答がタイムアウトしました。時間を置いて再度お試しください。"
        else:
            return f"ホロスコープの解釈中にネットワークエラーが発生しました。時間を置いて再度お試しください。"

@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    logger.info(f"Request body: {body}")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)
    except Exception as e:
        logger.error(f"Error handling webhook event: {e}")
        abort(500)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    logger.info(f"Received message: {event.message.text}")
    user_message = event.message.text.strip()
    user_id = event.source.user_id

    # Simple greeting
    if user_message.lower() == "こんにちは":
        reply_message = "こんにちは！無料でホロスコープを診断します。誕生年月日、出生時間（例: 1990-01-01 12:30）、そして出生地の都道府県と市区町村を教えてください。(例: 東京都, 港区)"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_message))
        logger.info("Sent greeting message.")
        return

    # Attempt to parse horoscope input
    logger.info(f"Attempting to parse horoscope input: '{user_message}'")
    try:
        parts = [p.strip() for p in user_message.split(',')]
        if len(parts) == 3:
            birth_date_str = parts[0].split(' ')[0]
            birth_time_str = parts[0].split(' ')[1]
            region = parts[1]
            city = parts[2]

            # Input validation for date and time format
            datetime.strptime(birth_date_str, "%Y-%m-%d")
            datetime.strptime(birth_time_str, "%H:%M")

            logger.info("Sent loading message to prevent webhook timeout.")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ご依頼ありがとうございます！ホロスコープの解釈を生成中です。少々お待ちください..."))

            lat, lon = get_coordinates(city, region)
            if lat is None or lon is None:
                reply_message = "出生地の座標を取得できませんでした。都道府県と市区町村が正しいかご確認ください。"
                line_bot_api.push_message(user_id, TextSendMessage(text=reply_message))
                logger.error("Failed to get coordinates.")
                return

            natal_positions = calculate_natal_chart(birth_date_str, birth_time_str, lat, lon)
            if natal_positions is None:
                reply_message = "ホロスコープの計算中にエラーが発生しました。入力情報をご確認ください。"
                line_bot_api.push_message(user_id, TextSendMessage(text=reply_message))
                logger.error("Failed to calculate natal chart.")
                return

            interpretation = generate_horoscope_interpretation(natal_positions)
            reply_message = f"ホロスコープ診断結果です：\n{interpretation}"
            line_bot_api.push_message(user_id, TextSendMessage(text=reply_message))
            logger.info("Pushed final horoscope reply to user %s", user_id)

        else:
            reply_message = "入力形式が正しくありません。例: 1990-01-01 12:30, 東京都, 港区"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_message))
            logger.warning("Invalid input format received.")

    except ValueError:
        reply_message = "日付または時間の形式が正しくありません。例: 1990-01-01 12:30, 東京都, 港区"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_message))
        logger.warning("Invalid date/time format received.")
    except Exception as e:
        logger.error(f"Error during horoscope processing: {e}")
        reply_message = "ホロスコープの解釈中に予期せぬエラーが発生しました。時間を置いて再度お試しください。"
        line_bot_api.push_message(user_id, TextSendMessage(text=reply_message))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
