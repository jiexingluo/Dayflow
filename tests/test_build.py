from build import _remove_foreign_icu_binaries


def test_build_removes_path_injected_icu_binaries(tmp_path):
    internal_dir = tmp_path / "_internal"
    internal_dir.mkdir()
    foreign_files = [
        internal_dir / "icuuc.dll",
        internal_dir / "icudt78.dll",
        internal_dir / "icuin78.dll",
    ]
    for path in foreign_files:
        path.write_bytes(b"foreign runtime")
    qt_runtime = internal_dir / "Qt6Core.dll"
    qt_runtime.write_bytes(b"qt runtime")

    _remove_foreign_icu_binaries(tmp_path)

    assert all(not path.exists() for path in foreign_files)
    assert qt_runtime.exists()
