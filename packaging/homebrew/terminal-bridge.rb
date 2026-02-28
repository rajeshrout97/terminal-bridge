# Homebrew formula for Terminal Bridge
# Install: brew tap <your-tap> && brew install terminal-bridge
# Or:      brew install --HEAD terminal-bridge

class TerminalBridge < Formula
  include Language::Python::Virtualenv

  desc "Model-agnostic remote Mac terminal access for AI agents"
  homepage "https://github.com/rajeshrout97/terminal-bridge"
  url "https://files.pythonhosted.org/packages/source/t/terminal-bridge/terminal-bridge-0.1.0.tar.gz"
  sha256 "PLACEHOLDER_SHA256"
  license "MIT"

  head "https://github.com/rajeshrout97/terminal-bridge.git", branch: "main"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  def post_install
    # Create config directory
    (var/"terminal-bridge").mkpath
  end

  def caveats
    <<~EOS
      Terminal Bridge has been installed!

      Quick start:
        # On the remote Mac (the one you want to control):
        tbridge setup remote

        # On the local Mac (where your AI runs):
        tbridge setup local <PAIRING_CODE>

      The agent can be started as a service:
        brew services start terminal-bridge

      For more info: tbridge --help
    EOS
  end

  service do
    run [opt_bin/"tbridge", "agent", "start", "--foreground"]
    keep_alive true
    log_path var/"log/terminal-bridge/agent.log"
    error_log_path var/"log/terminal-bridge/agent-error.log"
    working_dir var/"terminal-bridge"
  end

  test do
    assert_match "Terminal Bridge", shell_output("#{bin}/tbridge --version")
  end
end

