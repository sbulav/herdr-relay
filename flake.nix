{
  description = "herdr-relay — WebSocket relay for monitoring and approving herdr agents";

  # Pinned to a release branch on purpose. On nixos-unstable the relay's Python
  # and websockets versions moved with every `nix flake update`, which is how a
  # deployment drifts away from what the tests last ran against.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-darwin"
        "x86_64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        rec {
          herdr-relay = pkgs.callPackage ./nix/package.nix { };
          default = herdr-relay;
        }
      );

      overlays.default = final: _prev: {
        herdr-relay = final.callPackage ./nix/package.nix { };
      };

      # Generic on purpose: no hostnames, no secrets, no reverse proxy. See
      # docs/deployment.md for what the edge in front of this has to do, and
      # instantiate the module from your own configuration.
      nixosModules.herdr-relay = ./nix/module.nix;
      nixosModules.default = self.nixosModules.herdr-relay;

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (ps: [ ps.websockets ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.gnumake
              pkgs.nixfmt
              pkgs.nodejs
              python
              pkgs.ruff
            ];
          };
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (ps: [ ps.websockets ]);
        in
        {
          package = self.packages.${system}.herdr-relay;

          relay =
            pkgs.runCommand "herdr-relay-check"
              {
                nativeBuildInputs = [
                  pkgs.gnumake
                  pkgs.nodejs
                  python
                  pkgs.ruff
                ];
              }
              ''
                cp -R ${self} source
                chmod -R u+w source
                cd source
                # Importing the relay creates its log directory, and the default
                # falls back to $HOME — which in the sandbox is an unwritable
                # /homeless-shelter. The same trap catches a hardened unit; see
                # HERDR_LOG_DIR in docs/deployment.md.
                export HERDR_LOG_DIR="$TMPDIR/herdr-log"
                make check
                touch $out
              '';
        }
        # Rendering the unit is the cheap half of "did the deployment survive a
        # refactor": it catches a broken option, a tripped assertion, or a unit
        # that stopped referencing a credential it needs. Linux only — a systemd
        # unit does not build on darwin.
        // nixpkgs.lib.optionalAttrs (nixpkgs.lib.hasSuffix "-linux" system) {
          module =
            let
              sys = nixpkgs.lib.nixosSystem {
                inherit system;
                modules = [
                  self.nixosModules.herdr-relay
                  {
                    boot.loader.grub.enable = false;
                    fileSystems."/" = {
                      device = "/dev/null";
                      fsType = "ext4";
                    };
                    system.stateVersion = "25.11";

                    services.herdr-relay = {
                      enable = true;
                      tokenFile = "/run/secrets/herdr-relay-token";
                      remotes = [ "herdr@example" ];
                      ssh.identityFile = "/run/secrets/herdr-relay-ssh-key";
                      ssh.knownHostsFile = "/run/secrets/herdr-relay-known-hosts";
                    };
                  }
                ];
              };
              # A unit renders without consulting assertions — only
              # system.build.toplevel pulls those in, and building a whole system
              # to check one service is not worth it. Force them here instead.
              failures = map (a: a.message) (builtins.filter (a: !a.assertion) sys.config.assertions);
            in
            if failures != [ ] then
              throw (nixpkgs.lib.concatStringsSep "\n" failures)
            else
              sys.config.systemd.units."herdr-relay.service".unit;
        }
      );

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);
    };
}
