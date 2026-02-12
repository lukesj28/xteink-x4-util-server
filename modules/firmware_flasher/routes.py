from flask import render_template
from modules.firmware_flasher import bp


@bp.route("/")
def index():
    return render_template("firmware_flasher/index.html")
