// SPDX-License-Identifier: MIT
// tgen-agent: runs the emulated BGP routers of one cluster node.
//   tgen-agent --plan plan.json --out result.json        one run, timeline in the plan (CLI)
//   tgen-agent serve --listen ADDR:PORT --token-file F    service driven by the controller
#include <chrono>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <thread>

#include "tgen/run.hpp"

#ifndef TGEN_VERSION
#define TGEN_VERSION "dev"
#endif

namespace tgen {
int serve_main(const std::string& listen, const std::string& token_file, const std::string& default_iface,
               const std::string& version);
}

namespace {

void usage() {
  std::fprintf(stderr,
               "tgen-agent %s\n"
               "usage: tgen-agent --plan PLAN.json --out RESULT.json\n"
               "       tgen-agent serve --listen ADDRESS:PORT --token-file FILE [--iface eth0]\n"
               "       tgen-agent --version\n",
               TGEN_VERSION);
}

int cli(const std::string& plan_path, const std::string& out_path) {
  tgen::Plan plan;
  try {
    std::ifstream f(plan_path);
    if (!f) throw std::invalid_argument("cannot open " + plan_path);
    plan = tgen::plan_from_json(nlohmann::json::parse(f));
    if (!plan.timing.present) throw std::invalid_argument("CLI mode needs \"timing\" in the plan");
  } catch (const std::exception& e) {
    std::fprintf(stderr, "plan error: %s\n", e.what());
    return 2;
  }
  const auto end_ns = tgen::ms_to_ns(plan.timing.end_at);
  tgen::Run run(std::move(plan), TGEN_VERSION);
  std::fprintf(stderr, "tgen-agent %s: node %s, %zu routers, run %s\n", TGEN_VERSION, run.plan().node.c_str(),
               run.routers(), run.plan().run_id.c_str());
  run.start_cli();
  // wait until every router has stopped at end_at; safety net: end_at + 15 s
  while (true) {
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    if (tgen::now_ns() > end_ns && run.all_stopped()) break;
    if (tgen::now_ns() > end_ns + 15'000'000'000LL) {
      std::fprintf(stderr, "watchdog: forcing stop\n");
      run.stop();
      break;
    }
  }
  const auto out = run.result();
  std::ofstream o(out_path);
  o << out.dump() << "\n";
  if (!o) {
    std::fprintf(stderr, "cannot write %s\n", out_path.c_str());
    return 1;
  }
  std::fprintf(stderr, "result written to %s\n", out_path.c_str());
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc >= 2 && std::string(argv[1]) == "serve") {
    std::string listen, token_file = "/etc/tgen/agent.token", iface;
    for (int i = 2; i < argc; ++i) {
      std::string a = argv[i];
      if (a == "--listen" && i + 1 < argc) listen = argv[++i];
      else if (a == "--token-file" && i + 1 < argc) token_file = argv[++i];
      else if (a == "--iface" && i + 1 < argc) iface = argv[++i];
      else { usage(); return 2; }
    }
    if (listen.empty()) { usage(); return 2; }
    return tgen::serve_main(listen, token_file, iface, TGEN_VERSION);
  }
  std::string plan_path, out_path;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    if (a == "--plan" && i + 1 < argc) plan_path = argv[++i];
    else if (a == "--out" && i + 1 < argc) out_path = argv[++i];
    else if (a == "--version") { std::printf("%s\n", TGEN_VERSION); return 0; }
    else { usage(); return 2; }
  }
  if (plan_path.empty() || out_path.empty()) { usage(); return 2; }
  return cli(plan_path, out_path);
}
