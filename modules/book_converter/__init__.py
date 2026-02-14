from flask import Blueprint

bp = Blueprint(
    'book_converter',
    __name__,
    template_folder='templates',
    static_folder='static',
)

from modules.book_converter import routes
