"""Desktop shell for Katherine (pywebview proof, #334).

This package hosts the minimal Linux desktop shell used to validate
pywebview as the desktop base. It contains no domain logic: the backend
core remains importable and testable without a display, and importing
anything here never opens a window by itself.
"""
