from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).parent / "streamlit_app_v2.py"),
    run_name="__main__",
)
