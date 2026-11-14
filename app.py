from pathlib import Path
import base64
from email.message import EmailMessage
from email.utils import parseaddr
import json
import mimetypes
import os
import re
import smtplib
import ssl
import struct
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

from flask import Flask, render_template, request
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

ORDER_EMAIL = "modulixprint@gmail.com"
MAX_EMAIL_ATTACHMENT_BYTES = 18 * 1024 * 1024


class MailNotConfiguredError(Exception):
    """Raised when no supported email transport is configured."""


class AttachmentSizeError(Exception):
    """Raised when uploaded files exceed a safe email attachment size."""


def valid_email_address(value: str) -> bool:
    value = value.strip()
    if not value or len(value) > 254 or "\r" in value or "\n" in value:
        return False
    parsed = parseaddr(value)[1]
    if parsed != value or value.count("@") != 1 or any(char.isspace() for char in value):
        return False
    local_part, domain = value.rsplit("@", 1)
    return bool(local_part and "." in domain and not domain.startswith(".") and not domain.endswith("."))


def send_email_via_resend(
    subject: str,
    body: str,
    reply_to: str,
    attachments: list[tuple[str, Path]],
) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise MailNotConfiguredError

    payload = {
        "from": os.environ.get(
            "RESEND_FROM_EMAIL", "Modulix Print <onboarding@resend.dev>"
        ).strip(),
        "to": [os.environ.get("ORDER_EMAIL", ORDER_EMAIL).strip()],
        "subject": subject,
        "text": body,
        "reply_to": [reply_to],
    }
    if attachments:
        payload["attachments"] = []
        for filename, path in attachments:
            content_type, _ = mimetypes.guess_type(filename)
            payload["attachments"].append({
                "filename": filename,
                "content": base64.b64encode(path.read_bytes()).decode("ascii"),
                "content_type": content_type or "application/octet-stream",
            })

    request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ModulixPrint/1.0 (https://threed-print-website-15z0.onrender.com)",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=25) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Resend API returned HTTP {response.status}")
            response.read()
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Resend API returned HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach Resend API: {error.reason}") from error


def send_order_email(subject: str, body: str, reply_to: str, attachments=()) -> None:
    attachment_paths = [(filename, Path(path)) for filename, path in attachments]
    if sum(path.stat().st_size for _, path in attachment_paths) > MAX_EMAIL_ATTACHMENT_BYTES:
        raise AttachmentSizeError

    if os.environ.get("RESEND_API_KEY", "").strip():
        send_email_via_resend(subject, body, reply_to, attachment_paths)
        return

    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    if not smtp_user or not smtp_password:
        raise MailNotConfiguredError

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_user
    message["To"] = os.environ.get("ORDER_EMAIL", ORDER_EMAIL).strip()
    message["Reply-To"] = reply_to
    message.set_content(body)

    for filename, path in attachment_paths:
        content_type, _ = mimetypes.guess_type(filename)
        maintype, subtype = (content_type or "application/octet-stream").split("/", 1)
        message.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=filename
        )

    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(smtp_user, smtp_password)
        server.send_message(message)

ALLOWED_EXTENSIONS = {"stl", "3mf", "obj", "step", "stp"}

# Beispielwerte: Hier kannst du später deine echten Material- und Maschinenpreise
# zentral anpassen. Die Werte werden auf der Druckseite automatisch verwendet.
MATERIAL_PRICING = {
    "PLA": {
        "label": "PLA",
        "per_100g": 5.00,
        "per_hour": 1.50,
        "density": 1.24,
    },
    "PETG": {
        "label": "PETG",
        "per_100g": 5.00,
        "per_hour": 1.75,
        "density": 1.27,
    },
    "TPU": {
        "label": "TPU",
        "per_100g": 5.00,
        "per_hour": 2.00,
        "density": 1.21,
    },
}

# Grobe STL-Näherung bis ein Slicer Profil, Wände, Stützen und tatsächliche
# Druckzeit berechnet. Bei 100 % Füllung entspricht die Schätzung dem Vollmaterial.
STL_SHELL_VOLUME_FACTOR = 0.22
STL_VOLUME_PER_HOUR_CM3 = 8.0
STL_LAYER_HEIGHT_MM = 0.2
STL_LAYER_OVERHEAD_SECONDS = 3.0
STL_SETUP_HOURS = 0.15


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _triangle_volume(vertices: list[tuple[float, float, float]]) -> float:
    """Return the signed volume contribution of one triangle in mm³."""
    a, b, c = vertices
    cross = (
        b[1] * c[2] - b[2] * c[1],
        b[2] * c[0] - b[0] * c[2],
        b[0] * c[1] - b[1] * c[0],
    )
    return (a[0] * cross[0] + a[1] * cross[1] + a[2] * cross[2]) / 6.0


def estimate_stl_geometry(path: Path) -> tuple[float, float] | None:
    """Return an STL's estimated volume in cm³ and Z height in mm."""
    data = path.read_bytes()
    total = 0.0
    minimum_z = float("inf")
    maximum_z = float("-inf")

    if len(data) >= 84:
        triangle_count = struct.unpack_from("<I", data, 80)[0]
        expected_size = 84 + triangle_count * 50
        if triangle_count and expected_size <= len(data):
            for offset in range(84, expected_size, 50):
                values = struct.unpack_from("<12f", data, offset)
                vertices = [
                    (values[3], values[4], values[5]),
                    (values[6], values[7], values[8]),
                    (values[9], values[10], values[11]),
                ]
                for point in vertices:
                    minimum_z = min(minimum_z, point[2])
                    maximum_z = max(maximum_z, point[2])
                total += _triangle_volume(vertices)
            return abs(total) / 1000.0, maximum_z - minimum_z

    # Fallback for ASCII STL files.
    text = data.decode("utf-8", errors="ignore")
    matches = re.findall(
        r"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)",
        text,
        flags=re.IGNORECASE,
    )
    if len(matches) < 3:
        return None

    vertices = [(float(x), float(y), float(z)) for x, y, z in matches]
    minimum_z = min(point[2] for point in vertices)
    maximum_z = max(point[2] for point in vertices)
    for index in range(0, len(vertices) - 2, 3):
        total += _triangle_volume(vertices[index : index + 3])
    return abs(total) / 1000.0, maximum_z - minimum_z


def estimate_stl_volume(path: Path) -> float | None:
    """Compatibility helper returning only the STL volume in cm³."""
    geometry = estimate_stl_geometry(path)
    return geometry[0] if geometry else None


def estimate_stl_print(volume_cm3: float, height_mm: float, material: str, infill: str | float) -> dict:
    """Estimate material and print time from a mesh, material and infill setting."""
    material_key = material if material in MATERIAL_PRICING else "PLA"
    try:
        infill_percent = min(100.0, max(0.0, float(infill)))
    except (TypeError, ValueError):
        infill_percent = 15.0

    effective_fill = STL_SHELL_VOLUME_FACTOR + (1 - STL_SHELL_VOLUME_FACTOR) * infill_percent / 100
    estimated_weight = volume_cm3 * MATERIAL_PRICING[material_key]["density"] * effective_fill
    layer_count = max(1, height_mm / STL_LAYER_HEIGHT_MM)
    estimated_hours = (
        STL_SETUP_HOURS
        + volume_cm3 * effective_fill / STL_VOLUME_PER_HOUR_CM3
        + layer_count * STL_LAYER_OVERHEAD_SECONDS / 3600
    )
    return {
        "infill": round(infill_percent),
        "effective_fill": effective_fill,
        "estimated_weight": estimated_weight,
        "estimated_hours": estimated_hours,
    }


def calculate_estimate(
    material: str,
    weight_grams: str | float = 0,
    print_hours: str | float = 0,
    quantity: str | int = 1,
    delivery: str = "pickup",
) -> dict:
    material_key = material if material in MATERIAL_PRICING else "PLA"
    pricing = MATERIAL_PRICING[material_key]

    def number(value, fallback=0.0):
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return fallback

    weight = number(weight_grams)
    hours = number(print_hours)
    units = max(1, int(number(quantity, 1)))
    material_cost = (weight / 100) * pricing["per_100g"]
    machine_cost = hours * pricing["per_hour"]
    unit_total = material_cost + machine_cost
    shipping = 4.90 if delivery == "shipping" else 0.0

    return {
        "material": material_key,
        "weight": round(weight, 1),
        "hours": round(hours, 2),
        "quantity": units,
        "material_cost": round(material_cost, 2),
        "machine_cost": round(machine_cost, 2),
        "unit_total": round(unit_total, 2),
        "subtotal": round(unit_total * units, 2),
        "shipping": round(shipping, 2),
        "total": round(unit_total * units + shipping, 2),
    }


@app.context_processor
def inject_site_data():
    return {"material_pricing": MATERIAL_PRICING}


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/submit")
def submit():
    return render_template("submit.html")


@app.route("/scan")
def scan():
    return render_template("service.html", service="scan")


@app.route("/design")
def design():
    return render_template("service.html", service="design")


@app.route("/upload", methods=["POST"])
def upload():
    files = [file for file in request.files.getlist("files") if file and file.filename]
    if not files:
        legacy_file = request.files.get("file")
        if legacy_file and legacy_file.filename:
            files = [legacy_file]

    if not files:
        return render_template("submit.html", error="Bitte wähle zuerst eine 3D-Datei aus."), 400

    if any(not allowed_file(file.filename) for file in files):
        return render_template(
            "submit.html",
            error="Dieses Dateiformat wird noch nicht unterstützt. Erlaubt sind STL, 3MF, OBJ und STEP.",
        ), 400

    customer_name = request.form.get("customer_name", "").strip()
    customer_email = request.form.get("customer_email", "").strip()
    customer_note = request.form.get("customer_note", "").strip()
    if len(customer_name) > 120 or len(customer_note) > 5000 or not customer_name or not valid_email_address(customer_email):
        return render_template(
            "submit.html",
            error="Bitte gib einen Namen, eine gültige E-Mail-Adresse und eine höchstens 5.000 Zeichen lange Nachricht an.",
        ), 400

    delivery = request.form.get("delivery", "pickup")
    if delivery not in {"pickup", "shipping"}:
        delivery = "pickup"
    shipping_address = request.form.get("shipping_address", "").strip()
    shipping_postcode = request.form.get("shipping_postcode", "").strip()
    shipping_city = request.form.get("shipping_city", "").strip()
    shipping_country = request.form.get("shipping_country", "").strip()
    if delivery == "shipping" and not all((shipping_address, shipping_postcode, shipping_city, shipping_country)):
        return render_template(
            "submit.html",
            error="Für den Versand fülle bitte die vollständige Versandadresse aus.",
        ), 400

    materials = request.form.getlist("item_material")
    infills = request.form.getlist("item_infill")
    quantities = request.form.getlist("item_quantity")
    items = []
    material_total = 0.0
    machine_total = 0.0
    total_quantity = 0
    total_weight = 0.0
    saved_files = []

    for index, file in enumerate(files):
        original_filename = secure_filename(file.filename) or "3d-modell"
        stored_filename = f"{uuid.uuid4().hex[:8]}_{original_filename}"
        file_path = UPLOAD_FOLDER / stored_filename
        file.save(file_path)
        saved_files.append((original_filename, file_path))

        material = materials[index] if index < len(materials) else "PLA"
        quantity = quantities[index] if index < len(quantities) else "1"
        infill = infills[index] if index < len(infills) else "15"
        stl_data = None
        if file_path.suffix.lower() == ".stl":
            try:
                geometry = estimate_stl_geometry(file_path)
                if geometry is not None:
                    volume, height_mm = geometry
                    material_key = material if material in MATERIAL_PRICING else "PLA"
                    density = MATERIAL_PRICING[material_key]["density"]
                    print_estimate = estimate_stl_print(volume, height_mm, material_key, infill)
                    stl_data = {
                        "volume": round(volume, 2),
                        "solid_weight": round(volume * density, 1),
                        "estimated_weight": print_estimate["estimated_weight"],
                        "estimated_hours": print_estimate["estimated_hours"],
                        "infill": print_estimate["infill"],
                    }
            except (OSError, struct.error, ValueError):
                stl_data = None

        weight = stl_data["estimated_weight"] if stl_data else 0
        hours = stl_data["estimated_hours"] if stl_data else 0

        item_estimate = calculate_estimate(material, weight, hours, quantity, "pickup")
        material_total += item_estimate["material_cost"] * item_estimate["quantity"]
        machine_total += item_estimate["machine_cost"] * item_estimate["quantity"]
        total_quantity += item_estimate["quantity"]
        total_weight += item_estimate["weight"] * item_estimate["quantity"]
        items.append({
            "filename": original_filename,
            "material": item_estimate["material"],
            "weight": item_estimate["weight"],
            "hours": item_estimate["hours"],
            "infill": stl_data["infill"] if stl_data else 15,
            "quantity": item_estimate["quantity"],
            "material_cost": round(item_estimate["material_cost"] * item_estimate["quantity"], 2),
            "machine_cost": round(item_estimate["machine_cost"] * item_estimate["quantity"], 2),
            "stl_data": stl_data,
        })

    shipping = 4.90 if delivery == "shipping" else 0.0
    subtotal = round(material_total + machine_total, 2)
    estimate = {
        "material_cost": round(material_total, 2),
        "machine_cost": round(machine_total, 2),
        "subtotal": subtotal,
        "shipping": shipping,
        "quantity": total_quantity,
        "weight": round(total_weight, 1),
        "total": round(subtotal + shipping, 2),
        "complete": all(item["stl_data"] is not None for item in items),
    }

    order_reference = uuid.uuid4().hex[:8].upper()
    delivery_label = "Versand" if delivery == "shipping" else "Abholung"
    mail_lines = [
        f"Druckauftrag {order_reference}",
        "",
        f"Name: {customer_name}",
        f"E-Mail: {customer_email}",
        f"Übergabe: {delivery_label}",
    ]
    if delivery == "shipping":
        mail_lines.extend([
            "Versandadresse:",
            shipping_address,
            f"{shipping_postcode} {shipping_city}",
            shipping_country,
        ])
    if customer_note:
        mail_lines.extend(["", "Nachricht:", customer_note])
    mail_lines.extend(["", "Positionen:"])
    for index, item in enumerate(items, start=1):
        item_total = item["material_cost"] + item["machine_cost"]
        mail_lines.extend([
            f"{index}. {item['filename']}",
            f"   Material: {item['material']}",
            f"   Stückzahl: {item['quantity']}",
            f"   Füllung: {item['infill']} %",
        ])
        if item["stl_data"]:
            mail_lines.extend([
                f"   Geschätztes Gewicht pro Stück: {item['weight']:.1f} g",
                f"   Geschätzte Druckzeit pro Stück: {item['hours']:.2f} h",
                f"   Positionsschätzung: {item_total:.2f} €",
            ])
        else:
            mail_lines.append("   Gewicht und Preis: nach Prüfung der Datei")
    mail_lines.extend([
        "",
        f"Materialkosten gesamt: {estimate['material_cost']:.2f} €",
        f"Maschinenzeit gesamt: {estimate['machine_cost']:.2f} €",
        f"Versand: {estimate['shipping']:.2f} €",
        f"Gesamtschätzung: {estimate['total']:.2f} €" if estimate["complete"] else "Gesamtschätzung: nach Prüfung der Dateien",
        "",
        "Die angehängten Dateien sind die vom Kunden hochgeladenen 3D-Modelle.",
    ])

    mail_sent = False
    mail_error = ""
    try:
        send_order_email(
            f"[PrintLab] Druckauftrag {order_reference}",
            "\n".join(mail_lines),
            customer_email,
            saved_files,
        )
        mail_sent = True
    except MailNotConfiguredError:
        mail_error = "Der E-Mail-Versand ist auf dem Server noch nicht eingerichtet. Deine Anfrage wurde nicht abgeschickt."
    except AttachmentSizeError:
        mail_error = "Die Dateien sind zusammen zu groß für den E-Mail-Versand (maximal 18 MB). Bitte teile den Auftrag auf oder verkleinere die Dateien."
    except Exception:
        app.logger.exception("Could not send print order email")
        mail_error = "Der E-Mail-Versand ist gerade fehlgeschlagen. Deine Anfrage wurde nicht abgeschickt; bitte versuche es später erneut."
    finally:
        for _, file_path in saved_files:
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                app.logger.warning("Could not remove temporary upload %s", file_path.name)

    return render_template(
        "upload_success.html",
        items=items,
        estimate=estimate,
        customer_name=customer_name,
        order_reference=order_reference,
        mail_sent=mail_sent,
        mail_error=mail_error,
    )


@app.route("/service-request", methods=["POST"])
def service_request():
    service = request.form.get("service", "scan")
    if service not in {"scan", "design"}:
        service = "scan"

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    message = request.form.get("message", "").strip()
    mail_sent = False
    mail_error = ""

    if not name or not valid_email_address(email) or not message:
        mail_error = "Bitte gib Name, eine gültige E-Mail-Adresse und eine kurze Beschreibung an."
        return render_template(
            "request_received.html",
            service=service,
            name=name or "",
            mail_sent=False,
            mail_error=mail_error,
        ), 400

    service_label = "3D-Scan" if service == "scan" else "3D-Design"
    subject = f"[PrintLab] {service_label}-Anfrage"
    body = "\n".join([
        f"Neue {service_label}-Anfrage",
        "",
        f"Name: {name}",
        f"E-Mail: {email}",
        "",
        "Beschreibung:",
        message,
    ])
    try:
        send_order_email(subject, body, email)
        mail_sent = True
    except MailNotConfiguredError:
        mail_error = "Der E-Mail-Versand ist auf dem Server noch nicht eingerichtet. Deine Anfrage wurde nicht abgeschickt."
    except Exception:
        app.logger.exception("Could not send service request email")
        mail_error = "Der E-Mail-Versand ist gerade fehlgeschlagen. Deine Anfrage wurde nicht abgeschickt; bitte versuche es später erneut."

    return render_template(
        "request_received.html",
        service=service,
        name=name,
        mail_sent=mail_sent,
        mail_error=mail_error,
    )


if __name__ == "__main__":
    app.run(debug=True)
