// SPDX-License-Identifier: MIT
#include "tgen/run.hpp"

#include <pthread.h>
#include <sched.h>
#include <sys/resource.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <future>
#include <map>
#include <stdexcept>

namespace tgen {

using nlohmann::json;

PhaseCmd phase_from_json(const json& j) {
  static const std::map<std::string, PhaseType> types = {
      {"establish", PhaseType::Establish}, {"announce", PhaseType::Announce}, {"steady", PhaseType::Steady},
      {"churn", PhaseType::Churn},         {"flap", PhaseType::Flap},         {"withdraw", PhaseType::Withdraw},
      {"teardown", PhaseType::Teardown},   {"end", PhaseType::End}};
  PhaseCmd c;
  const auto type = j.value("type", "");
  auto it = types.find(type);
  if (it == types.end()) throw std::invalid_argument("phase: unknown type \"" + type + "\"");
  c.type = it->second;
  if (!j.contains("at_ms")) throw std::invalid_argument("phase: missing at_ms");
  c.at_ns = ms_to_ns(j.at("at_ms").get<std::int64_t>());
  const json p = j.value("params", json::object());
  c.rate = p.value("rate", 0.0);
  if (c.rate < 0) throw std::invalid_argument("phase: rate must be >= 0");
  if (p.contains("mix")) {
    const json& m = p["mix"];
    c.mix_announce = m.value("announce", 0.0);
    c.mix_withdraw = m.value("withdraw", 0.0);
    c.mix_attr = m.value("attr", 0.0);
    if (c.mix_announce < 0 || c.mix_withdraw < 0 || c.mix_attr < 0 || c.mix_announce + c.mix_withdraw + c.mix_attr <= 0)
      throw std::invalid_argument("phase: churn mix must be >= 0 and not all 0");
  }
  const auto kind = p.value("kind", "reset");
  c.flap = kind == "silent" ? FlapKind::Silent : kind == "withdraw_all" ? FlapKind::WithdrawAll : FlapKind::Reset;
  if (kind != "reset" && kind != "silent" && kind != "withdraw_all") throw std::invalid_argument("phase: flap kind " + kind);
  c.down_s = p.value("down_s", 5.0);
  c.close = p.value("close", false);
  // establish: per_s stagger; flap: routers (names) - both resolved by the Run
  c.offset_ns = static_cast<std::int64_t>(p.value("per_s", 0.0) * 1000);  // carries per_s x 1000 until resolved
  return c;
}

Run::Run(Plan plan, std::string version) : plan_(std::move(plan)), version_(std::move(version)) {
  const std::size_t n = std::max<std::size_t>(1, plan_.cores.size());
  for (std::size_t i = 0; i < n; ++i) {
    ios_.push_back(std::make_unique<asio::io_context>(1));
    guards_.push_back(asio::make_work_guard(*ios_.back()));
  }
  for (std::size_t i = 0; i < plan_.routers.size(); ++i) {
    const std::int64_t offset =
        plan_.timing.establish_per_s > 0 ? static_cast<std::int64_t>(i / plan_.timing.establish_per_s * 1e9) : 0;
    routers_.push_back(std::make_shared<Router>(*ios_[i % n], plan_, plan_.routers[i], offset));
  }
  for (std::size_t i = 0; i < n; ++i) {
    threads_.emplace_back([&io = *ios_[i]] { io.run(); });
    if (!plan_.cores.empty()) {
      cpu_set_t set;
      CPU_ZERO(&set);
      CPU_SET(plan_.cores[i], &set);
      if (pthread_setaffinity_np(threads_.back().native_handle(), sizeof(set), &set) != 0)
        std::fprintf(stderr, "warning: could not pin thread to core %d\n", plan_.cores[i]);
    }
  }
  t_start_ = now_ns();
}

Run::~Run() {
  try { stop(); } catch (...) {}
  for (auto& g : guards_) g.reset();
  for (auto& io : ios_) io->stop();
  for (auto& t : threads_)
    if (t.joinable()) t.join();
}

void Run::on_each_io(std::function<void(Router&)> fn) {
  // shared state: if a thread does not answer in time, its late task must not touch this stack frame
  struct Shared {
    std::function<void(Router&)> fn;
    std::vector<std::promise<void>> done;
  };
  const std::size_t n = ios_.size();
  auto sh = std::make_shared<Shared>();
  sh->fn = std::move(fn);
  sh->done.resize(n);
  std::vector<std::future<void>> wait;
  for (auto& d : sh->done) wait.push_back(d.get_future());
  for (std::size_t i = 0; i < n; ++i) {
    std::vector<std::shared_ptr<Router>> mine;
    for (std::size_t k = i; k < routers_.size(); k += n) mine.push_back(routers_[k]);
    asio::post(*ios_[i], [sh, i, mine = std::move(mine)] {
      for (auto& r : mine) sh->fn(*r);
      sh->done[i].set_value();
    });
  }
  for (auto& w : wait)
    if (w.wait_for(std::chrono::seconds(5)) != std::future_status::ready)
      throw std::runtime_error("router thread did not answer within 5 s");
}

void Run::start_cli() {
  on_each_io([](Router& r) { r.start(); });
}

void Run::schedule(const PhaseCmd& cmd) {
  // establish stagger: router i starts i / per_s seconds after at_ms
  const double per_s = cmd.type == PhaseType::Establish ? static_cast<double>(cmd.offset_ns) / 1000 : 0;
  auto order = std::make_shared<std::map<const Router*, std::size_t>>();
  for (std::size_t i = 0; i < routers_.size(); ++i) (*order)[routers_[i].get()] = i;
  on_each_io([cmd, per_s, order](Router& r) {
    PhaseCmd c = cmd;
    c.offset_ns = per_s > 0 ? static_cast<std::int64_t>(static_cast<double>(order->at(&r)) / per_s * 1e9) : 0;
    r.schedule(c);
  });
}

void Run::schedule_for(const PhaseCmd& cmd, const std::vector<std::string>& names) {
  on_each_io([cmd, names](Router& r) {
    for (const auto& n : names)
      if (n == r.name()) r.schedule(cmd);
  });
}

void Run::stop() {
  on_each_io([](Router& r) { r.stop_now(); });
}

bool Run::all_stopped() {
  auto all = std::make_shared<std::atomic<bool>>(true);
  on_each_io([all](Router& r) { if (!r.stopped()) *all = false; });
  return *all;
}

json Run::stats() {
  auto order = std::make_shared<std::map<const Router*, std::size_t>>();
  for (std::size_t i = 0; i < routers_.size(); ++i) (*order)[routers_[i].get()] = i;
  auto shared = std::make_shared<std::vector<LiveSnapshot>>(routers_.size());
  on_each_io([shared, order](Router& r) { (*shared)[order->at(&r)] = r.snapshot(); });
  auto& snaps = *shared;
  json by_state = json::object(), per_router = json::array(), tx = json::array(), rx = json::array();
  std::uint64_t sent = 0, wd = 0, upd = 0, updrx = 0, nlri = 0, wdrx = 0, held = 0, looped = 0;
  int conv = 0, resets = 0, planned = 0;
  double lag = 0;
  for (auto& s : snaps) {
    by_state[state_name(s.state)] = by_state.value(state_name(s.state), 0) + 1;
    sent += s.prefixes_sent; wd += s.prefixes_withdrawn; upd += s.updates_sent; updrx += s.updates_rx;
    nlri += s.nlri_rx; wdrx += s.withdrawn_rx; held += s.held; looped += s.looped_rx;
    conv += s.converged ? 1 : 0; resets += s.resets; planned += s.planned_resets;
    lag = std::max(lag, s.send_lag_p99_ms);
    per_router.push_back({{"name", s.name}, {"state", state_name(s.state)}, {"held", s.held},
                          {"sent", s.prefixes_sent}, {"withdrawn", s.prefixes_withdrawn}, {"rx", s.nlri_rx},
                          {"resets", s.resets}, {"planned_resets", s.planned_resets}, {"converged", s.converged}});
    for (const auto& x : s.tx) tx.push_back({x.key, x.gen, x.kind, x.t});
    for (const auto& x : s.rx) rx.push_back({x.key, x.gen, x.kind, x.t});
  }
  return {{"seq", ++seq_}, {"t_ms", now_ns() / 1'000'000}, {"by_state", by_state},
          {"totals", {{"prefixes_sent", sent}, {"prefixes_withdrawn", wd}, {"updates_sent", upd}, {"updates_rx", updrx},
                      {"nlri_rx", nlri}, {"withdrawn_rx", wdrx}, {"routes_held", held}, {"converged", conv},
                      {"resets", resets}, {"planned_resets", planned}, {"looped_rx", looped},
                      {"send_lag_p99_ms", lag}}},
          {"per_router", per_router}, {"samples", {{"tx", tx}, {"rx", rx}}}};
}

json Run::result() {
  auto order = std::make_shared<std::map<const Router*, std::size_t>>();
  for (std::size_t i = 0; i < routers_.size(); ++i) (*order)[routers_[i].get()] = i;
  auto shared = std::make_shared<std::vector<json>>(routers_.size());
  on_each_io([shared, order](Router& r) { (*shared)[order->at(&r)] = r.result(); });
  auto& rs = *shared;
  rusage ru{};
  getrusage(RUSAGE_SELF, &ru);
  json out = {
      {"run_id", plan_.run_id}, {"node", plan_.node}, {"version", version_},
      {"t_start", t_start_}, {"t_end", now_ns()}, {"threads", ios_.size()}, {"sample_mod", plan_.sample_mod},
      {"cpu_user_s", ru.ru_utime.tv_sec + ru.ru_utime.tv_usec / 1e6},
      {"cpu_sys_s", ru.ru_stime.tv_sec + ru.ru_stime.tv_usec / 1e6},
      {"max_rss_mb", ru.ru_maxrss / 1024.0},
      {"routers", rs},
  };
  return out;
}

}  // namespace tgen
