from flask import Blueprint

bp = Blueprint(
    'firmware_flasher',
    __name__,
    template_folder='templates',
    static_folder='static',
)

from modules.firmware_flasher import routes  # noqa: E402, F401
