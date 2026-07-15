from codex_move_session.paths import PathMapper


def test_posix_replaces_root_and_descendants_inside_text() -> None:
    mapper = PathMapper("/work/old", "/work/new", flavor="posix")

    text, count = mapper.replace_text(
        "cwd=/work/old; file=/work/old/src/app.py; keep=/work/old-copy"
    )

    assert text == "cwd=/work/new; file=/work/new/src/app.py; keep=/work/old-copy"
    assert count == 2


def test_posix_matching_is_case_sensitive() -> None:
    mapper = PathMapper("/Work/Old", "/Work/New", flavor="posix")

    text, count = mapper.replace_text("/work/old /Work/Old")

    assert text == "/work/old /Work/New"
    assert count == 1


def test_windows_handles_case_slashes_and_unc_descendants() -> None:
    drive = PathMapper(r"C:\Users\Alice\Old", r"D:\Projects\New", flavor="windows")
    unc = PathMapper(r"\\server\share\old", r"\\server\share\new", flavor="windows")

    drive_text, drive_count = drive.replace_text(
        r"c:/users/alice/old/src C:\Users\Alice\Old-copy"
    )
    unc_text, unc_count = unc.replace_text(r"\\SERVER\share\old\docs")

    assert drive_text == r"D:/Projects/New/src C:\Users\Alice\Old-copy"
    assert drive_count == 1
    assert unc_text == r"\\server\share\new\docs"
    assert unc_count == 1


def test_map_path_preserves_descendant_suffix() -> None:
    mapper = PathMapper("/old/project", "/new/project", flavor="posix")

    assert mapper.map_path("/old/project/subdir") == "/new/project/subdir"
    assert mapper.map_path("/old/project-two") is None


def test_windows_preserves_extended_path_prefixes() -> None:
    drive = PathMapper(r"\\?\C:\old", r"D:\new", flavor="windows")
    unc = PathMapper(
        r"\\?\UNC\server\share\old",
        r"\\server\share\new",
        flavor="windows",
    )

    drive_text, _ = drive.replace_text(r"\\?\C:\old\src")
    unc_text, _ = unc.replace_text(r"\\?\UNC\server\share\old\src")

    assert drive_text == r"\\?\D:\new\src"
    assert unc_text == r"\\?\UNC\server\share\new\src"
