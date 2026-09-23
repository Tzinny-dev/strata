"""Entry point for PyInstaller — absolute import avoids relative import failure in __main__.py"""
from strata.cli import main
import sys
sys.exit(main())
