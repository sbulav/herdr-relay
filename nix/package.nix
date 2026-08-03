{
  lib,
  stdenvNoCC,
  python3,
  makeWrapper,
  openssh,
  # Web Push exists only for the browser PWA in web/ and is retired with it
  # (#14). The relay already degrades to a warning when pywebpush is missing, so
  # this stays off by default rather than dragging cryptography into the closure.
  withWebPush ? false,
  # mDNS advertises LAN discovery no current client uses; #16 deletes it. Kept
  # on until then so the packaged relay behaves like `uv run` does.
  withZeroconf ? true,
}:
let
  version = "0.7.0";

  pythonEnv = python3.withPackages (
    ps:
    [ ps.websockets ]
    ++ lib.optional withZeroconf ps.zeroconf
    ++ lib.optionals withWebPush [
      ps.pywebpush
      ps.py-vapid
    ]
  );
in
stdenvNoCC.mkDerivation {
  pname = "herdr-relay";
  inherit version;

  # web/ is here because the relay resolves its LEGACY (#14) static routes
  # relative to its own file — see the installPhase layout below.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../relay/herdr_relay.py
      ../relay/on_event.py
      ../web
    ];
  };

  nativeBuildInputs = [ makeWrapper ];

  dontConfigure = true;
  dontBuild = true;

  # The relay reports its own version to clients on connect. If the derivation
  # and RELAY_VERSION disagree, `relay_version` on the wire is a lie, so fail
  # the build instead of shipping it.
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    grep -q 'RELAY_VERSION = "${version}"' relay/herdr_relay.py || {
      echo "RELAY_VERSION in relay/herdr_relay.py does not match package version ${version}" >&2
      exit 1
    }
    runHook postCheck
  '';

  # Layout matters: herdr_relay.py finds web/ at ../web relative to itself.
  installPhase = ''
    runHook preInstall

    mkdir -p $out/share/herdr-relay/relay
    cp relay/herdr_relay.py relay/on_event.py $out/share/herdr-relay/relay/
    cp -R web $out/share/herdr-relay/web

    makeWrapper ${pythonEnv}/bin/python3 $out/bin/herdr-relay \
      --add-flags $out/share/herdr-relay/relay/herdr_relay.py \
      --prefix PATH : ${lib.makeBinPath [ openssh ]}

    runHook postInstall
  '';

  meta = {
    description = "WebSocket relay for monitoring and approving herdr AI agents remotely";
    homepage = "https://github.com/sbulav/herdr-relay";
    license = lib.licenses.agpl3Plus;
    mainProgram = "herdr-relay";
    platforms = lib.platforms.unix;
  };
}
