class LocalDevProxy < Formula
  desc "Detached local-dev Caddy service with Python orchestration CLI"
  homepage "https://github.com/andrewtheguy/zellij-test"
  license "MIT"
  head "https://github.com/andrewtheguy/zellij-test.git", branch: "main"

  depends_on "caddy"
  depends_on "uv"

  def install
    pkgshare.install "pyproject.toml"
    pkgshare.install "README.md"
    pkgshare.install "config.env"
    pkgshare.install "routes.toml"
    pkgshare.install "config"
    pkgshare.install "layouts"
    pkgshare.install "scripts"
    pkgshare.install "src"

    (bin/"local-dev-proxy").write <<~EOS
      #!/usr/bin/env bash
      set -euo pipefail
      export LOCAL_DEV_PROXY_ROOT="#{opt_pkgshare}"
      exec "#{Formula["uv"].opt_bin}/uv" run --project "#{opt_pkgshare}" local-dev-proxy "$@"
    EOS
  end

  service do
    run [
      Formula["caddy"].opt_bin/"caddy",
      "run",
      "--config",
      opt_pkgshare/"config/caddy-bootstrap.json",
    ]
    keep_alive true
    log_path var/"log/local-dev-proxy-caddy.log"
    error_log_path var/"log/local-dev-proxy-caddy.log"
    environment_variables(
      XDG_DATA_HOME: "#{HOMEBREW_PREFIX}/var/lib",
      HOME:          "#{HOMEBREW_PREFIX}/var/lib",
    )
  end

  def caveats
    <<~EOS
      Before starting this service, stop upstream caddy to avoid port conflicts:
        brew services stop caddy

      Start detached proxy:
        brew services start local-dev-proxy
    EOS
  end

  test do
    assert_predicate(opt_pkgshare/"config/caddy-bootstrap.json", :exist?)
    assert_predicate(opt_pkgshare/"routes.toml", :exist?)
  end
end
