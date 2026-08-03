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
}:
let
  version = "0.7.0";

  pythonEnv = python3.withPackages (
    ps:
    [ ps.websockets ]
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
      ../relay/herdr-relay.py
      ../relay/herdr_relay
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
    grep -q 'RELAY_VERSION = "${version}"' relay/herdr_relay/__init__.py || {
      echo "RELAY_VERSION in relay/herdr_relay/__init__.py does not match package version ${version}" >&2
      exit 1
    }
    runHook postCheck
  '';

  # Layout matters: the package resolves web/ two levels up from itself, and the
  # launcher imports the package from its own directory.
  installPhase = ''
    runHook preInstall

    mkdir -p $out/share/herdr-relay/relay
    cp relay/herdr-relay.py relay/on_event.py $out/share/herdr-relay/relay/
    cp -R relay/herdr_relay $out/share/herdr-relay/relay/herdr_relay
    cp -R web $out/share/herdr-relay/web

    makeWrapper ${pythonEnv}/bin/python3 $out/bin/herdr-relay \
      --add-flags $out/share/herdr-relay/relay/herdr-relay.py \
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
