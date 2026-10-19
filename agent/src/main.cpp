// SPDX-License-Identifier: MIT
// tgen-agent: runs the emulated BGP routers of one cluster node.
//   tgen-agent --plan plan.json --out result.json
// The plan carries absolute timestamps, so all nodes act on one timeline.
#include <pthread.h>
#include <sched.h>
#include <sys/resource.h>

#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <memory>
#include <thread>
#include <vector>

#include "tgen/plan.hpp"
#include "tgen/router.hpp"

#ifndef TGEN_VERSION
#define TGEN_VERSION "dev"
#endif

namespace {

void usage() {
  std::fprintf(stderr,
               "tgen-agent %s\n"
               "usage: tgen-agent --plan PLAN.json --out RESULT.json\n"
               "       tgen-agent --version\n",
               TGEN_VERSION);
}

void pin(std::thread& t, int core) {
  cpu_set_t set;
  CPU_ZERO(&set);
  CPU_SET(core, &set);
  if (pthread_setaffinity_np(t.native_handle(), sizeof(set), &set) != 0)
    std::fprintf(stderr, "warning: could not pin thread to core %d\n", core);
}

}  // namespace

int main(int argc, char** argv) {
  std::string plan_path, out_path;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    if (a == "--plan" && i + 1 < argc) plan_path = argv[++i];
    else if (a == "--out" && i + 1 < argc) out_path = argv[++i];
    else if (a == "--version") { std::printf("%s\n", TGEN_VERSION); return 0; }
    else { usage(); return 2; }
  }
  if (plan_path.empty() || out_path.empty()) { usage(); return 2; }

  tgen::Plan plan;
  try {
    std::ifstream f(plan_path);
    if (!f) throw std::invalid_argument("cannot open " + plan_path);
    plan = tgen::plan_from_json(nlohmann::json::parse(f));
  } catch (const std::exception& e) {
    std::fprintf(stderr, "plan error: %s\n", e.what());
    return 2;
  }

  const std::size_t nthreads = std::max<std::size_t>(1, plan.cores.size());
  std::vector<std::unique_ptr<asio::io_context>> ios;
  for (std::size_t i = 0; i < nthreads; ++i) ios.push_back(std::make_unique<asio::io_context>(1));

  std::vector<std::shared_ptr<tgen::Router>> routers;
  for (std::size_t i = 0; i < plan.routers.size(); ++i) {
    const std::int64_t offset =
        plan.timing.establish_per_s > 0 ? static_cast<std::int64_t>(i / plan.timing.establish_per_s * 1e9) : 0;
    routers.push_back(std::make_shared<tgen::Router>(*ios[i % nthreads], plan, plan.routers[i], offset));
  }
  for (auto& r : routers) r->start();

  std::fprintf(stderr, "tgen-agent %s: node %s, %zu routers, %zu threads, run %s\n", TGEN_VERSION, plan.node.c_str(),
               routers.size(), nthreads, plan.run_id.c_str());
  const std::int64_t t_start = tgen::now_ns();

  std::vector<std::thread> threads;
  for (std::size_t i = 0; i < nthreads; ++i) {
    threads.emplace_back([&io = *ios[i]] { io.run(); });
    if (!plan.cores.empty()) pin(threads.back(), plan.cores[i]);
  }
  // safety net: never run past end_at + 15 s, whatever happens
  std::thread watchdog([&] {
    while (tgen::now_ns() < tgen::ms_to_ns(plan.timing.end_at) + 15'000'000'000LL) {
      bool all_done = true;
      for (auto& io : ios) all_done &= io->stopped();
      if (all_done) return;
      std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
    std::fprintf(stderr, "watchdog: forcing stop\n");
    for (auto& io : ios) io->stop();
  });
  for (auto& t : threads) t.join();
  watchdog.join();

  rusage ru{};
  getrusage(RUSAGE_SELF, &ru);
  nlohmann::json out = {
      {"run_id", plan.run_id}, {"node", plan.node}, {"version", TGEN_VERSION},
      {"t_start", t_start}, {"t_end", tgen::now_ns()}, {"threads", nthreads}, {"sample_mod", plan.sample_mod},
      {"cpu_user_s", ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6},
      {"cpu_sys_s", ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6},
      {"max_rss_mb", ru.ru_maxrss / 1024.0},
      {"routers", nlohmann::json::array()},
  };
  for (auto& r : routers) out["routers"].push_back(r->result());
  std::ofstream o(out_path);
  o << out.dump() << "\n";
  if (!o) {
    std::fprintf(stderr, "cannot write %s\n", out_path.c_str());
    return 1;
  }
  std::fprintf(stderr, "result written to %s\n", out_path.c_str());
  return 0;
}
