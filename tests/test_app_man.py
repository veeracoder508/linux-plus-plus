import sys
from linux_plus_plus.app import ManSystem, MAN_PAGES


def test_man_missing_topic(capsys):
    rc = ManSystem.show("this_topic_does_not_exist")
    assert rc == 1


def test_man_show_topic_windows(monkeypatch, capsys):
    # Force Windows mode to avoid interactive pager
    import linux_plus_plus.app as appmod
    monkeypatch.setattr(appmod, 'IS_WINDOWS', True)

    rc = ManSystem.show('ls')
    assert rc == 0
    captured = capsys.readouterr()
    assert 'ls' in captured.out or 'list directory contents' in captured.out
