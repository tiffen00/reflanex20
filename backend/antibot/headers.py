"""Score HTTP headers for bot-like behaviour."""

from starlette.datastructures import Headers


def score_headers(headers: Headers) -> int:
    """
    Return a suspicion score based on HTTP headers.
    Higher score = more bot-like.
    Score >= 3 → considered a bot.
    """
    score = 0

    ua = headers.get("user-agent", "")
    if not ua:
        score += 3  # missing UA is highly suspicious

    accept_lang = headers.get("accept-language", "")
    if not accept_lang or accept_lang.strip() == "*":
        score += 2

    accept = headers.get("accept", "")
    # Only flag if Accept is completely absent (not just */*)
    # since */⁠* is a valid Accept value for many legitimate clients
    if not accept:
        score += 1

    accept_enc = headers.get("accept-encoding", "")
    if not accept_enc:
        score += 1

    # Modern browsers always send Sec-Fetch-* headers
    sec_dest = headers.get("sec-fetch-dest", "")
    sec_mode = headers.get("sec-fetch-mode", "")
    sec_site = headers.get("sec-fetch-site", "")
    if not sec_dest and not sec_mode and not sec_site:
        score += 1

    # Suspicious custom headers used by scanners
    suspect_headers = ("x-scanner", "x-forensic", "x-security-scan", "x-malware-scan")
    for sh in suspect_headers:
        if headers.get(sh):
            score += 5

    return score
