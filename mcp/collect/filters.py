"""Literal hard filters. Unknown/negotiable salary passes; no AI scoring."""
import re


def salary_lower_k(text):
    text = text.replace(',', '').strip().lower()
    if any(word in text for word in ('面议', '/天', '/时', '/小时', '每天', '每小时', '日薪', '时薪')):
        return None
    # Monthly K, monthly 万, annual 万 and explicit monthly yuan only.
    match = re.search(r'(\d+(?:\.\d+)?)\s*(?:[-~～—至]\s*\d+(?:\.\d+)?)?\s*(k|千|万|元)', text)
    if not match:
        return None
    value, unit = float(match[1]), match[2]
    if unit == '万':
        value *= 10
    elif unit == '元':
        value /= 1000
    if '年' in text[:match.end()+3] and ('/年' in text or '万/年' in text or '年薪' in text):
        value /= 12
    return value


def filter_reasons(listing, settings):
    reasons = []
    salary = salary_lower_k(listing.salary)
    if salary is not None and salary < settings.salary_min_k:
        reasons.append('salary_below_min')
    if any(w.casefold() in listing.title.casefold() for w in settings.title_exclude if w.strip()):
        reasons.append('title_excluded')
    if any(w.casefold() in listing.company.casefold() for w in settings.company_exclude if w.strip()):
        reasons.append('company_excluded')
    if any(w.casefold() in listing.company.casefold() for w in settings.blocked_companies if w.strip()):
        reasons.append('company_blocked')
    return reasons
