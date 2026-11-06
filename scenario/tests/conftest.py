"""Pytest configuration for the e-commerce scenario."""

import os
import sys

# Add scenario/src to the Python path for testing
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))
