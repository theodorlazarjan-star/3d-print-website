from pathlib import Path
import re
import struct
import uuid

from flask import Flask, render_template, request
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

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

    materials = request.form.getlist("item_material")
    infills = request.form.getlist("item_infill")
    quantities = request.form.getlist("item_quantity")
    delivery = request.form.get("delivery", "pickup")
    items = []
    material_total = 0.0
    machine_total = 0.0
    total_quantity = 0
    total_weight = 0.0

    for index, file in enumerate(files):
        original_filename = secure_filename(file.filename)
        stored_filename = f"{uuid.uuid4().hex[:8]}_{original_filename}"
        file_path = UPLOAD_FOLDER / stored_filename
        file.save(file_path)

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

    return render_template(
        "upload_success.html",
        items=items,
        estimate=estimate,
    )


@app.route("/service-request", methods=["POST"])
def service_request():
    service = request.form.get("service", "scan")
    if service not in {"scan", "design"}:
        service = "scan"

    return render_template(
        "request_received.html",
        service=service,
        name=request.form.get("name", "").strip() or "dein Name",
    )


if __name__ == "__main__":
    app.run(debug=True)
