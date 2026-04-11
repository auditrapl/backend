"""
Email Service — Resend integration for Auditra.
Wysyłka Morning Briefing i powiadomień.
"""
import os
import logging
import resend
from datetime import datetime

logger = logging.getLogger("email_service")

resend.api_key = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "cfo@auditra.pl")


def _urgency_color(urgency: str) -> str:
    return {"high": "#ef4444", "medium": "#f59e0b", "low": "#10b981"}.get(urgency, "#6b7280")


def _urgency_label(urgency: str) -> str:
    return {"high": "PILNE", "medium": "WAŻNE", "low": "INFO"}.get(urgency, "INFO")


def _urgency_emoji(urgency: str) -> str:
    """Emoji ikona urgency — działa wszędzie (Outlook, Gmail, Apple Mail)."""
    return {"high": "&#9888;&#65039;", "medium": "&#128200;", "low": "&#9989;"}.get(urgency, "&#8226;")


def _bold_numbers(text: str) -> str:
    """Pogrubia wartości finansowe w tekście (kwoty PLN, procenty, tygodnie, dni)."""
    import re
    # Match: "130 472 PLN", "57%", "15 tygodni", "62 dni", "35 000 PLN/mies.", "3x"
    text = re.sub(
        r'(\d[\d\s,]*(?:\.\d+)?\s*(?:PLN(?:/mies\.?)?|%|tygodni|tygodnie|dni|msc|miesięcy|x)\b)',
        r'<strong>\1</strong>',
        text
    )
    return text


def build_briefing_html(
    company_name: str,
    summary: str,
    insights: list[dict],
) -> str:
    """Buduje HTML emaila Morning Briefing — full white, card-based layout."""

    # Insight cards — osobne karty z emerald accent
    insight_cards = ""
    for i, ins in enumerate(insights[:3]):
        urgency = ins.get("urgency", "low")
        color = _urgency_color(urgency)
        label = _urgency_label(urgency)
        emoji = _urgency_emoji(urgency)
        bg = {"high": "#fef2f2", "medium": "#fffbeb", "low": "#f0fdf4"}.get(urgency, "#f9fafb")
        border = {"high": "#fecaca", "medium": "#fde68a", "low": "#bbf7d0"}.get(urgency, "#e5e7eb")
        title_bold = _bold_numbers(ins.get('title', ''))
        desc_bold = _bold_numbers(ins.get('description', ''))

        insight_cards += f"""
        <div style="background: #ffffff; border-radius: 12px; padding: 20px; margin-bottom: 12px;">
            <table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
                <td width="44" valign="top" style="padding-right: 14px;">
                    <div style="width: 40px; height: 40px; background: {bg}; border-radius: 10px; text-align: center; line-height: 40px; font-size: 18px;">
                        {emoji}
                    </div>
                </td>
                <td valign="top">
                    <div style="margin-bottom: 4px;">
                        <span style="font-size: 10px; font-weight: 700; color: {color}; letter-spacing: 0.8px; text-transform: uppercase;">{label}</span>
                    </div>
                    <div style="font-size: 15px; font-weight: 600; color: #111827; margin-bottom: 6px; line-height: 1.4;">{title_bold}</div>
                    <div style="font-size: 13px; color: #6b7280; line-height: 1.6; margin: 0;">{desc_bold}</div>
                </td>
            </tr></table>
        </div>
        <div style="height: 1px; background: #f3f4f6; margin: 0 20px 12px 20px;"></div>
        """

    date_str = datetime.now().strftime('%d.%m.%Y')
    day_names = {0: 'Poniedziałek', 1: 'Wtorek', 2: 'Środa', 3: 'Czwartek', 4: 'Piątek', 5: 'Sobota', 6: 'Niedziela'}
    day_name = day_names.get(datetime.now().weekday(), '')

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; background: #ffffff; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; -webkit-font-smoothing: antialiased;">
    <div style="max-width: 560px; margin: 0 auto; padding: 48px 24px;">

        <!-- Logo + date bar -->
        <table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom: 32px;">
            <tr>
                <td>
                    <div style="font-size: 18px; font-weight: 700; color: #111827; letter-spacing: -0.3px;">auditra</div>
                </td>
                <td align="right">
                    <div style="font-size: 12px; color: #9ca3af;">{day_name}, {date_str}</div>
                </td>
            </tr>
        </table>

        <!-- Divider -->
        <div style="height: 1px; background: #f3f4f6; margin-bottom: 32px;"></div>

        <!-- Greeting -->
        <div style="margin-bottom: 28px;">
            <div style="font-size: 13px; font-weight: 500; color: #10b981; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 8px;">Poranna odprawa</div>
            <h1 style="font-size: 24px; font-weight: 700; color: #111827; margin: 0; line-height: 1.3;">{company_name}</h1>
        </div>

        <!-- Summary card -->
        <div style="background: #f9fafb; border-radius: 12px; padding: 24px; margin-bottom: 32px;">
            <div style="font-size: 11px; font-weight: 600; color: #10b981; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px;">Podsumowanie</div>
            <p style="font-size: 14px; color: #374151; line-height: 1.7; margin: 0;">{_bold_numbers(summary)}</p>
        </div>

        <!-- Section label -->
        <div style="font-size: 11px; font-weight: 600; color: #9ca3af; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 16px;">Sygnały dnia</div>

        <!-- Insight cards -->
        {insight_cards}

        <!-- CTA -->
        <div style="text-align: center; margin: 36px 0;">
            <a href="https://app.auditra.pl/dashboard/cfo" style="display: inline-block; background: #111827; color: #ffffff; padding: 14px 40px; border-radius: 10px; text-decoration: none; font-size: 14px; font-weight: 600; letter-spacing: 0.2px;">Otwórz dashboard</a>
        </div>

        <!-- Divider -->
        <div style="height: 1px; background: #f3f4f6; margin-bottom: 24px;"></div>

        <!-- Footer -->
        <table cellpadding="0" cellspacing="0" border="0" width="100%">
            <tr>
                <td>
                    <div style="font-size: 11px; color: #d1d5db;">auditra.pl</div>
                </td>
                <td align="right">
                    <div style="font-size: 11px; color: #d1d5db;">Wygenerowane przez CFO AI</div>
                </td>
            </tr>
        </table>

    </div>
</body>
</html>"""


def send_briefing_email(
    to_email: str,
    company_name: str,
    summary: str,
    insights: list[dict],
) -> bool:
    """Wysyła Morning Briefing emailem przez Resend. Zwraca True jeśli sukces."""
    if not resend.api_key:
        logger.warning("[Email] RESEND_API_KEY not set — skipping email")
        return False

    html = build_briefing_html(company_name, summary, insights)

    try:
        result = resend.Emails.send({
            "from": f"Auditra CFO <{FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"Poranna Odprawa — {company_name} · {datetime.now().strftime('%d.%m.%Y')}",
            "html": html,
        })
        logger.info(f"[Email] Briefing sent to {to_email} — id={result.get('id', '?')}")
        return True
    except Exception as e:
        logger.error(f"[Email] Failed to send to {to_email}: {e}", exc_info=True)
        return False


def send_welcome_email(
    to_email: str,
    dashboard_url: str = "https://app.auditra.pl/dashboard",
) -> bool:
    """Send welcome email after auto-provisioning from Stripe checkout."""
    if not resend.api_key:
        logger.warning("[Email] RESEND_API_KEY not set — skipping welcome email")
        return False

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#ffffff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:560px;margin:0 auto;padding:48px 24px;">
    <div style="font-size:18px;font-weight:700;color:#111827;margin-bottom:32px;">auditra</div>
    <div style="height:1px;background:#f3f4f6;margin-bottom:32px;"></div>

    <h1 style="font-size:24px;font-weight:700;color:#111827;margin:0 0 16px;">Witaj w Auditra!</h1>
    <p style="font-size:15px;color:#374151;line-height:1.7;">
        Twoja subskrypcja jest aktywna. Za chwile otrzymasz od Supabase email z linkiem do ustawienia hasla.
    </p>
    <p style="font-size:15px;color:#374151;line-height:1.7;">
        Po ustawieniu hasla, zaloguj sie i skonfiguruj firme:
    </p>

    <div style="text-align:center;margin:36px 0;">
        <a href="{dashboard_url}" style="display:inline-block;background:#1D9E75;color:#ffffff;padding:14px 40px;border-radius:10px;text-decoration:none;font-size:14px;font-weight:600;">Przejdz do dashboardu</a>
    </div>

    <div style="background:#f0fdf4;border-radius:12px;padding:20px;margin-bottom:24px;">
        <p style="font-size:13px;color:#166534;margin:0;font-weight:600;">Co dalej?</p>
        <ol style="font-size:13px;color:#374151;line-height:1.8;margin:8px 0 0;padding-left:20px;">
            <li>Ustaw haslo (email od Supabase)</li>
            <li>Zaloguj sie do dashboardu</li>
            <li>Uzupelnij dane firmy (NIP, forma prawna)</li>
            <li>Wgraj faktury CSV lub podlacz KSeF</li>
        </ol>
    </div>

    <div style="height:1px;background:#f3f4f6;margin-bottom:24px;"></div>
    <div style="font-size:11px;color:#d1d5db;">auditra.pl &middot; AI Dyrektor Finansowy</div>
</div>
</body>
</html>"""

    try:
        result = resend.Emails.send({
            "from": f"Auditra <{FROM_EMAIL}>",
            "to": [to_email],
            "subject": "Witaj w Auditra! Ustaw haslo i zacznij.",
            "html": html,
        })
        logger.info(f"[Email] Welcome email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"[Email] Failed welcome email to {to_email}: {e}")
        return False
