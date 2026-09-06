"""
API Route Module
"""

from flask import Blueprint

graph_bp = Blueprint('graph', __name__)
simulation_bp = Blueprint('simulation', __name__)
report_bp = Blueprint('report', __name__)

def register_research_routes():
    from . import graph, simulation, report  # noqa: F401
