class StrataLang < Formula
  desc "Strata — declarative, versioned data transformations (strata-lang)"
  homepage "https://github.com/Tzinny-dev/strata"
  license "MIT"
  # NOTE: version and sha256 are replaced by CI on tag push (see .github/workflows/binary.yml release job)
  # To update manually: brew bump-formula-pr --tag=v0.1.1 strata-lang
  version "0.1.2"
  if OS.mac?
    url "https://github.com/Tzinny-dev/strata/releases/download/v0.1.2/strata-macos-amd64"
    sha256 "32a9dffd93c17a2d575e87d3abc492326a9e0071447bb5328ed33448054f6e45"
  elsif OS.linux?
    url "https://github.com/Tzinny-dev/strata/releases/download/v0.1.2/strata-linux-amd64"
    sha256 "f45e7b518f39fb390f6fc448cea4bfc691d5d1a96aa07b2225d1ef34d9f794b1"
  end

  def install
    bin.install Dir["strata*"].first => "strata"
    # completions / man if added later
  end

  test do
    system "#{bin}/strata", "--help"
    system "#{bin}/strata", "build", "--help"
  end
end
