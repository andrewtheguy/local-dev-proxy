class LocalDevProxy < Formula
  desc "Detached local-dev Caddy service with Python orchestration CLI"
  homepage "https://github.com/andrewtheguy/zellij-test"
  license "MIT"
  repo_root = Pathname.new(__FILE__).realpath.dirname.parent
  head "file://#{repo_root}", branch: "main", using: :git

  depends_on "caddy"

  def install
    pkgshare.install "config/caddy-bootstrap.json"
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
      Start detached proxy:
        brew services start local-dev-proxy

      This service uses dedicated ports:
        HTTP: 2810
        Admin API: 2020
    EOS
  end

  test do
    assert_predicate(opt_pkgshare/"config/caddy-bootstrap.json", :exist?)
  end
end
