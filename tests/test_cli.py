from cli.forge import build_parser


def test_cli_has_required_commands():
    parser = build_parser()
    subparsers_action = next(action for action in parser._actions if action.dest == "command")

    assert set(subparsers_action.choices) == {"login", "run", "logs", "publish", "resolve", "ls"}
