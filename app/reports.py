from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def incident_pdf(incident: dict[str, Any]) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Incident Report - {incident['title']}",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Muted", parent=styles["BodyText"], textColor=colors.HexColor("#52606d")))
    story = [
        Paragraph("INCIDENT MEMORY AGENT", styles["Muted"]),
        Paragraph(incident["title"], styles["Title"]),
        Spacer(1, 5 * mm),
    ]
    metadata = [
        ["Service", incident["service"], "Severity", incident["severity"]],
        ["Status", incident["status"].title(), "Classification", incident["novelty"].title()],
        ["Category", incident["error_category"], "Created", incident["created_at"][:19]],
    ]
    table = Table(metadata, colWidths=[26 * mm, 55 * mm, 30 * mm, 48 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e8eef7")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#e8eef7")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c4cedb")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend([table, Spacer(1, 6 * mm)])
    sections = [
        ("Summary", _safe(incident["description"])),
        ("Agent Diagnosis", _safe(incident["diagnosis"])),
        ("Root Cause", _safe(incident["root_cause"])),
        ("Impact", _safe(incident["impact"])),
        (
            "Deployment Source",
            _safe(
                "Repository: "
                f"{incident.get('repository') or 'Not recorded'}\n"
                f"Commit: {incident.get('commit_sha') or 'Not recorded'}\n"
                f"File: {incident.get('file_path') or 'Not identified'}"
            ),
        ),
        ("Error Evidence", _safe(incident["error_logs"] or "No logs supplied.")),
        ("Failing Code", _safe(incident.get("code_snippet") or "Not identified.")),
        ("Proposed Fix", _safe(incident.get("fix_summary") or "Not generated.")),
        ("Fix Rationale", _safe(incident.get("fix_rationale") or "Not generated.")),
        ("Unified Diff", _safe(incident.get("fix_diff") or "Not generated.")),
        ("Resolution Steps", _list_text(incident["resolution_steps"])),
        ("Action Items", _list_text(incident["action_items"])),
        ("Timeline", _timeline_text(incident["timeline"])),
        ("Similar Past Incidents", _memory_text(incident["retrieved_memories"])),
    ]
    for heading, content in sections:
        story.append(Paragraph(heading, styles["Heading2"]))
        story.append(Paragraph(content, styles["BodyText"]))
        story.append(Spacer(1, 3 * mm))
    document.build(story)
    return buffer.getvalue()


def _safe(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def _list_text(values: list[str]) -> str:
    return "<br/>".join(f"- {_safe(value)}" for value in values) or "Not recorded."


def _timeline_text(values: list[dict[str, Any]]) -> str:
    return "<br/>".join(
        f"- {_safe(str(item.get('at', ''))[:19])}: {_safe(str(item.get('event', '')))}"
        for item in values
    ) or "Not recorded."


def _memory_text(values: list[dict[str, Any]]) -> str:
    if not values:
        return "No similar incident exceeded the retrieval threshold."
    return "<br/>".join(
        f"- {int(item['similarity'] * 100)}%: {_safe(item['incident']['title'])}"
        for item in values
    )
