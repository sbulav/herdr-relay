# Wire contract

`native/*.json` is one golden file per frame the relay puts on the wire, in the
native dialect described by [`../docs/native-protocol.md`](../docs/native-protocol.md).

The files are **generated, not hand-written**. `tests/test_native_contract.py`
drives the real emitters (`_poll_once`, `launch_session`, `terminate_session`,
`wake_host`, `shutdown_host`) against fakes and compares their output to these
files. Editing a frame in `relay/herdr_relay/` fails the suite here, before
the change reaches a phone.

After an *intentional* protocol change, regenerate and review the diff:

```bash
UPDATE_CONTRACT=1 nix develop --command python -m unittest discover -s tests -p 'test_*.py'
git diff -- contract/native
```

A diff you did not expect is a contract break. `herdr-mobile` parses these
frames (`protocol/LegacyProtocol.kt`) and copies these files in as fixtures, so
a break here is a break there.
