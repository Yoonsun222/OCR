from raglab.cli import main


def test_no_command_returns_operator_input_error(capsys):
    assert main([]) == 2
    assert "usage: raglab" in capsys.readouterr().err
