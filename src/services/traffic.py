"""Conservative signatures: do not classify people by IP, country or network."""

import re

KNOWN_AUTOMATION = re.compile(
    r"googlebot|google-inspectiontool|bingbot|bingpreview|yandexbot|yandeximages|"
    r"duckduckbot|baiduspider|bytespider|petalbot|applebot|gptbot|oai-searchbot|"
    r"chatgpt-user|claudebot|claude-user|anthropic-ai|perplexitybot|perplexity-user|"
    r"ccbot|ahrefsbot|semrushbot|mj12bot|dotbot|blexbot|dataforseobot|"
    r"facebookexternalhit|meta-externalagent|meta-externalfetcher|twitterbot|"
    r"slackbot|discordbot|telegrambot|linkedinbot|pinterestbot|^whatsapp/|"
    r"infrawatch|censysinspect|cyberconvoyscout|zgrab|masscan|nmap scripting engine|"
    r"headlesschrome|lighthouse|pagespeed|uptimerobot|pingdom|statuscake|"
    r"(?:^|\W)(?:bot|crawler|spider|scraper)(?:\W|$)|"
    r"^(?:curl|wget|python-requests|python-httpx|python-urllib|go-http-client|"
    r"apache-httpclient|libwww-perl)/|^yurets-.*verification",
    re.IGNORECASE,
)


def known_bot(user_agent: str) -> bool:
    return bool(KNOWN_AUTOMATION.search((user_agent or "")[:2048]))
