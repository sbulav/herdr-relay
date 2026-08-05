# NixOS module for herdr-relay.
#
# Deliberately generic: no hostnames, no secrets, no reverse proxy. Instantiate
# it from your own configuration and supply secrets as files (sops-nix, agenix,
# whatever) — every secret here is read through systemd credentials, which is
# what makes it work under DynamicUser, where no dynamic uid can own a file in
# /run/secrets.
#
# The edge (TLS, public hostname, proxy) is intentionally out of scope; see
# docs/deployment.md for what a proxy in front of this must do.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.herdr-relay;

  stateDir = "/var/lib/herdr-relay";
  logDir = "/var/log/herdr-relay";

  usesRemotes = cfg.remotes != [ ];
  # A host file can carry SSH targets independently of the legacy remotes
  # option, so it needs the same credential and ~/.ssh setup.
  usesSsh = usesRemotes || cfg.hostsFile != null;

  # SSH is invoked as a plain `ssh` with no -i, so the identity has to come from
  # a config file in $HOME. Under DynamicUser $HOME is the state directory, so
  # the file is rewritten on every start from the credentials systemd unpacked.
  sshSetup = pkgs.writeShellScript "herdr-relay-ssh-setup" ''
    set -euo pipefail
    install -d -m 700 "$HOME/.ssh"
    {
      echo "Host *"
      echo "  IdentitiesOnly yes"
      echo "  IdentityFile $CREDENTIALS_DIRECTORY/ssh-key"
      echo "  UserKnownHostsFile $CREDENTIALS_DIRECTORY/ssh-known-hosts"
      echo "  StrictHostKeyChecking yes"
      echo "  BatchMode yes"
    } > "$HOME/.ssh/config"
    chmod 600 "$HOME/.ssh/config"
  '';

  # The token reaches the relay as an environment variable, and it must not carry
  # a trailing newline: the relay compares it with hmac.compare_digest, so a
  # stray byte reads as a wrong token. Command substitution strips them.
  startScript = pkgs.writeShellScript "herdr-relay-start" ''
    set -euo pipefail
    export HERDR_RELAY_TOKEN="$(cat "$CREDENTIALS_DIRECTORY/token")"
    exec ${lib.getExe cfg.package}
  '';
in
{
  options.services.herdr-relay = {
    enable = lib.mkEnableOption "the herdr-relay WebSocket relay";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./nix/package.nix { }";
      description = ''
        The herdr-relay package to run. Defaults to building it from this
        module's own sources against the host's nixpkgs, so no overlay is
        required; set it to `inputs.herdr-relay.packages.''${system}.herdr-relay`
        to use the version pinned by this repo's flake instead.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8375;
      description = ''
        Port for the combined WebSocket and HTTP listener. The relay binds
        0.0.0.0; put a TLS-terminating proxy in front of it rather than exposing
        this directly.
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Open {option}`services.herdr-relay.port` in the firewall. Leave this off
        when a reverse proxy on the same host is the only client.
      '';
    };

    tokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/herdr-relay-token";
      description = ''
        File containing the shared secret clients authenticate with, as raw
        contents (a trailing newline is stripped). Required — the relay refuses
        to start without a token. Passed in as a systemd credential, so the file
        only has to be readable by root.
      '';
    };

    herdrBin = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/run/current-system/sw/bin/herdr";
      description = ''
        Absolute path to the herdr binary. When unset the relay looks for
        `herdr` on the unit's PATH and otherwise falls back to a Homebrew path
        that does not exist on NixOS, in which case every local poll reports the
        host offline.
      '';
    };

    remotes = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "herdr@builder" ];
      description = ''
        SSH targets to poll in addition to the local host. These are login
        strings and never reach a client — the relay strips them at the
         broadcast boundary. Setting this, or using a hostsFile with SSH
         targets, requires
        {option}`services.herdr-relay.ssh.identityFile` and
        {option}`services.herdr-relay.ssh.knownHostsFile`.
      '';
    };

    ssh = {
      identityFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        example = "/run/secrets/herdr-relay-ssh-key";
        description = ''
           Private key used to reach {option}`services.herdr-relay.remotes` or
           SSH targets in {option}`services.herdr-relay.hostsFile`.
          Passed in as a systemd credential.
        '';
      };

      knownHostsFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        example = "/etc/ssh/herdr_known_hosts";
        description = ''
           `known_hosts` file for remotes and hosts-file targets. Required alongside
          {option}`services.herdr-relay.ssh.identityFile`: the relay runs ssh
          with `BatchMode=yes`, so an unverified host key fails the poll rather
          than prompting.
        '';
      };
    };

    presetsFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        JSON file of launch presets. Each preset's `target` is an SSH login
        string, so this is treated as a secret and passed in as a systemd
        credential.
      '';
    };

    hostsFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Versioned host configuration JSON. It contains private SSH routing,
        wrapper paths, project roots, and power addresses, so it is passed as a
        systemd credential rather than made world-readable.
      '';
    };

    power = {
      hostId = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = ''
          Legacy preset-mode host id for `wake_host` and `shutdown_host`.
          Host-file deployments declare power capabilities per host instead.
          Both legacy operations are refused unless this and
          {option}`services.herdr-relay.power.hostMac` are set.
        '';
      };

      hostMac = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "00:11:22:33:44:55";
        description = "MAC address woken by `wake_host`.";
      };

      wakeBin = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "/run/current-system/sw/bin/wakeonlan";
        description = ''
          Absolute path to the wake-on-LAN binary. Defaults to `wakeonlan` on
          the unit's PATH.
        '';
      };
    };

    environmentFile = lib.mkOption {
      type = lib.types.listOf lib.types.path;
      default = [ ];
      description = ''
        Extra `KEY=value` files loaded into the unit's environment, read by root
        before privileges are dropped. Use this for settings not covered by an
        option above; use {option}`services.herdr-relay.tokenFile` for the
        token.
      '';
    };

    extraEnvironment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = {
        HERDR_CLAUDE_PROJECTS = "/var/lib/herdr-relay/claude-projects";
      };
      description = ''
        Additional non-secret environment variables. See docs/deployment.md for
        the full set the relay reads.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.tokenFile != null;
        message = ''
          services.herdr-relay.tokenFile must be set — the relay refuses to
          start without HERDR_RELAY_TOKEN.
        '';
      }
      {
         assertion = usesSsh -> (cfg.ssh.identityFile != null && cfg.ssh.knownHostsFile != null);
         message = ''
           services.herdr-relay uses SSH targets, so both ssh.identityFile and
           ssh.knownHostsFile are required: the relay polls hosts with
           BatchMode=yes and no fallback to an interactive prompt.
        '';
      }
      {
        assertion = (cfg.power.hostId == null) == (cfg.power.hostMac == null);
        message = ''
          services.herdr-relay.power.hostId and power.hostMac must be set
          together when using legacy preset-mode power controls.
        '';
      }
    ];

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

    systemd.services.herdr-relay = {
      description = "herdr-relay WebSocket relay";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      environment = lib.filterAttrs (_: v: v != null) (
        {
          HERDR_RELAY_PORT = toString cfg.port;
          HERDR_LOG_DIR = logDir;
          HERDR_PROJECTS_DB = "${stateDir}/projects.sqlite3";
          HOME = stateDir;
          HERDR_BIN = cfg.herdrBin;
          HERDR_REMOTES = if usesRemotes then lib.concatStringsSep "," cfg.remotes else null;
          HERDR_PRESETS_FILE = if cfg.presetsFile != null then "%d/presets" else null;
          HERDR_HOSTS_FILE = if cfg.hostsFile != null then "%d/hosts" else null;
          HERDR_POWER_HOST_ID = cfg.power.hostId;
          HERDR_POWER_HOST_MAC = cfg.power.hostMac;
          HERDR_WAKE_BIN = cfg.power.wakeBin;
        }
        // cfg.extraEnvironment
      );

      serviceConfig = {
        ExecStart = startScript;
         ExecStartPre = lib.optional usesSsh sshSetup;
        Restart = "on-failure";
        RestartSec = "5s";

        LoadCredential = [
          "token:${cfg.tokenFile}"
        ]
         ++ lib.optionals usesSsh [
          "ssh-key:${cfg.ssh.identityFile}"
          "ssh-known-hosts:${cfg.ssh.knownHostsFile}"
        ]
        ++ lib.optional (cfg.presetsFile != null) "presets:${cfg.presetsFile}"
        ++ lib.optional (cfg.hostsFile != null) "hosts:${cfg.hostsFile}";

        EnvironmentFile = cfg.environmentFile;

        # $HOME lives here: ssh needs a writable config, and under DynamicUser
        # the state directory is the only place that qualifies.
        DynamicUser = true;
        StateDirectory = "herdr-relay";
        StateDirectoryMode = "0700";
        LogsDirectory = "herdr-relay";
        WorkingDirectory = stateDir;

        AmbientCapabilities = "";
        CapabilityBoundingSet = "";
        DevicePolicy = "closed";
        LockPersonality = true;
        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateTmp = true;
        # The relay reads a human's ~/.claude/projects and OpenCode database when
        # it polls the local host. Under DynamicUser it cannot reach another
        # user's home anyway, so this only makes that explicit — point
        # HERDR_CLAUDE_PROJECTS / HERDR_OPENCODE_DB at readable copies, or
        # lib.mkForce this to "read-only" and grant access deliberately.
        ProtectHome = true;
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectProc = "invisible";
        ProtectSystem = "strict";
        RemoveIPC = true;
        # AF_NETLINK outlived the mDNS advertisement it was added for: glibc's
        # resolver enumerates interfaces over netlink for AI_ADDRCONFIG, and
        # polling a remote resolves its name through ssh in this same cgroup.
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
          "AF_NETLINK"
        ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          "~@privileged"
          "~@resources"
        ];
        UMask = "0077";
      };
    };
  };
}
