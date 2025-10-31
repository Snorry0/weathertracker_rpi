def test_import_and_help(tmp_path):
    import subprocess, sys
    p = subprocess.run([sys.executable, "-m", "meteocli.cli", "-h"], capture_output=True)
    assert p.returncode == 0
    assert b"CLI meteo" in p.stdout
