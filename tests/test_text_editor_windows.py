import builtins
from linux_plus_plus.apps.text_editor import TextEditor
import linux_plus_plus.apps.text_editor as te_mod


def test_text_editor_windows_save_and_quit(tmp_path, monkeypatch, capsys):
    # Create initial file
    p = tmp_path / "doc.txt"
    p.write_text("first\nsecond\n")

    # Force windows mode for the module
    monkeypatch.setattr(te_mod, 'IS_WINDOWS', True)

    inputs = iter([
        ':w',  # save
        ':q',  # quit
    ])

    def fake_input(prompt=''):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(builtins, 'input', fake_input)

    editor = TextEditor(str(p))
    rc = editor.run()
    assert rc == 0
    # file still exists and contains the text
    content = p.read_text(encoding='utf-8')
    assert 'first' in content
