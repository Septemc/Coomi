#!/usr/bin/env python3
"""Coomi Agent — Textual UI 入口"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from apps.backend.core.ui.textual_app import CoomiApp


def main():
    app = CoomiApp()
    app.run()


if __name__ == "__main__":
    main()
