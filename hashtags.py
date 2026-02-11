"""
Hashtag detection — categorize Polymarket trades by topic.
Uses keyword matching on market title + Gamma API tags as fallback.
"""
import logging
import re

logger = logging.getLogger(__name__)

# ── Keyword → Hashtag mapping ──────────────────────────────────
# Order matters: first match wins. More specific patterns first.
KEYWORD_MAP = [
    # Politics
    (r"\b(trump|biden|harris|obama|desantis|pence|haley|newsom|kennedy|rfk|vivek|vance|aoc|pelosi|mcconnell)\b", "#політика"),
    (r"\b(president|election|congress|senate|governor|democrat|republican|gop|dem|primary|electoral|inaugur|impeach|vote|ballot|poll)\b", "#політика"),
    (r"\b(white house|supreme court|cabinet|attorney general|secretary of state)\b", "#політика"),

    # Crypto
    (r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|doge|cardano|ada|polygon|matic|bnb|avalanche|avax|litecoin|ltc)\b", "#крипто"),
    (r"\b(crypto|defi|nft|blockchain|token|altcoin|stablecoin|memecoin|web3)\b", "#крипто"),
    (r"\b(coinbase|binance|kraken|ftx|tether|usdc|usdt)\b", "#крипто"),

    # Sports / Betting
    (r"\b(nfl|nba|mlb|nhl|ufc|mma|epl|premier league|champions league|la liga|serie a|bundesliga|mls|fifa|f1|formula 1|nascar)\b", "#спорт"),
    (r"\b(super bowl|world series|world cup|stanley cup|playoffs|championship|finals|match|game \d|round \d)\b", "#спорт"),
    (r"\b(lakers|celtics|warriors|chiefs|eagles|cowboys|yankees|dodgers|arsenal|liverpool|manchester|barcelona|real madrid)\b", "#спорт"),
    (r"\b(tennis|golf|olympics|boxing|wrestling)\b", "#спорт"),

    # Stocks / Finance
    (r"\b(tsla|aapl|googl|goog|amzn|msft|nvda|meta|nflx|amd|intc|dis|ba|jpm|gs|spy|qqq|dow|nasdaq|s&p)\b", "#акції"),
    (r"\b(stock|share price|market cap|ipo|earnings|revenue|quarterly|annual report|fed rate|interest rate|inflation|gdp|cpi)\b", "#акції"),
    (r"\b(tesla|apple|google|amazon|microsoft|nvidia|netflix|disney|boeing)\b", "#акції"),
    (r"\b(close at \$|open at \$|trading of the week|trading day)\b", "#акції"),

    # Weather
    (r"\b(temperature|weather|hurricane|tornado|earthquake|flood|wildfire|storm|snow|rain|heat|cold|drought|celsius|fahrenheit)\b", "#погода"),
    (r"\b(highest temp|lowest temp|record high|record low)\b", "#погода"),

    # AI / Tech
    (r"\b(openai|chatgpt|gpt-?[45]|claude|anthropic|gemini|llama|ai model|artificial intelligence|machine learning|agi|deepmind)\b", "#ai"),
    (r"\b(tech|startup|silicon valley|venture capital|vc funding)\b", "#tech"),

    # Culture / Entertainment
    (r"\b(oscar|grammy|emmy|golden globe|academy award|box office|movie|film|album|song|spotify|youtube|tiktok|twitter|x\.com)\b", "#культура"),
    (r"\b(celebrity|kanye|drake|taylor swift|beyonce|rihanna|elon musk|jeff bezos|mark zuckerberg)\b", "#культура"),

    # Geopolitics / War
    (r"\b(ukraine|russia|china|taiwan|iran|israel|palestine|gaza|nato|war|invasion|sanctions|ceasefire|peace deal|missile)\b", "#геополітика"),

    # Science / Health
    (r"\b(covid|vaccine|pandemic|fda|who|virus|disease|cancer|clinical trial|drug approval|space|mars|moon|nasa|spacex|launch)\b", "#наука"),
]

# Compile patterns once
_COMPILED = [(re.compile(pattern, re.IGNORECASE), tag) for pattern, tag in KEYWORD_MAP]


def detect_hashtag(title: str, tags: list[str] | None = None) -> str:
    """
    Detect hashtag for a market based on its title.
    Returns the most specific hashtag found.
    """
    if not title:
        return "#інше"

    # Check keyword patterns
    for pattern, tag in _COMPILED:
        if pattern.search(title):
            return tag

    # Fallback: check Gamma API tags
    if tags:
        tag_map = {
            "politics": "#політика",
            "crypto": "#крипто",
            "sports": "#спорт",
            "finance": "#акції",
            "weather": "#погода",
            "ai": "#ai",
            "tech": "#tech",
            "culture": "#культура",
            "science": "#наука",
            "pop-culture": "#культура",
        }
        for t in tags:
            t_lower = t.lower()
            if t_lower in tag_map:
                return tag_map[t_lower]

    return "#інше"


def get_hashtag_emoji(hashtag: str) -> str:
    """Return an emoji for the hashtag."""
    emojis = {
        "#політика": "🏛",
        "#крипто": "₿",
        "#спорт": "⚽",
        "#акції": "📈",
        "#погода": "🌡",
        "#ai": "🤖",
        "#tech": "💻",
        "#культура": "🎬",
        "#геополітика": "🌍",
        "#наука": "🔬",
        "#інше": "📋",
    }
    return emojis.get(hashtag, "📋")
