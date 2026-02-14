from flask import Blueprint

bp = Blueprint(
    'manga_formatter',
    __name__,
    template_folder='templates',
    static_folder='static',
)

from modules.manga_formatter import routes
