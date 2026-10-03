"""Conservative discovery hints. These never authorize redaction."""
import re

HOSTILE = re.compile(r"\b(fuck(?:ing|ed|er)?|shit|asshole|idiot|stupid|dumbass|useless|moron|bitch|trash|hate)\b", re.I)
USEFUL = re.compile(
    r"\b(why|how|what|where|when|which|can|could|would|please|stop|fix|undo|restore|"
    r"change|update|use|keep|preserve|don't|do not|never|instead|because|should|"
    r"answer|explain|write|quote|translate|example|test|file|code|error|column|"
    r"port|path|client|project|instruction|requirement|question|output)\b|[0-9`/\\]", re.I
)
PRAISE = re.compile(r"\b(thanks?|thank you|awesome|great|love|excellent|good|amazing)\b", re.I)
DIRECTED = re.compile(r"\b(you|you're|your|ai|claude|chatgpt)\b", re.I)
BARE_INSULT = re.compile(r"^(?:fucking\s+)?(?:idiot|moron|dumbass|asshole|bitch)[.!?\s]*$", re.I)


def candidate_reason(text):
    """Prefer false negatives to stripping useful corrections or quoted text."""
    if not HOSTILE.search(text):
        return None
    if len(text) > 350 or USEFUL.search(text) or PRAISE.search(text):
        return None
    if any(mark in text for mark in ('"', '\u201c', '\u201d', '\n>')):
        return None
    if text.lstrip().startswith(("<", ">", "```")):
        return None
    if DIRECTED.search(text) or BARE_INSULT.fullmatch(text.strip()):
        return "possible directed insult without an obvious useful request; review required"
    return None
