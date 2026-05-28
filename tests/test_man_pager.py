import builtins
import linux_plus_plus.app as appmod
from linux_plus_plus.app import ManSystem


def test_man_pager_quit(monkeypatch, capsys):
    # Force non-windows to hit pager codepath
    monkeypatch.setattr(appmod, 'IS_WINDOWS', False)

    # Make terminal small so pager iterates
    import linux_plus_plus.apps.text_editor as te_mod
    monkeypatch.setattr(te_mod.TextEditor, '_term_size', staticmethod(lambda: (40, 5)))

    # Simulate user pressing 'q' at first prompt
    inputs = iter(['q'])
    monkeypatch.setattr(builtins, 'input', lambda prompt='': next(inputs))

    rc = ManSystem.show('ls')
    assert rc == 0
    captured = capsys.readouterr()
    assert 'list directory contents' in captured.out
