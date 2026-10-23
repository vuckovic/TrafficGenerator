// SPDX-License-Identifier: MIT
// tgen-agent serve: the agent as a service, driven by the controller over HTTP
// on the management network (docs/07-agent-api.md).
#include <sys/wait.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <fstream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <httplib.h>

#include "tgen/garp.hpp"
#include "tgen/run.hpp"

namespace tgen {

namespace {

using nlohmann::json;

// fork/exec, no shell: arguments are validated strings (IPv4, interface name)
int run_cmd(const std::vector<std::string>& args) {
  std::vector<char*> argv;
  for (const auto& a : args) argv.push_back(const_cast<char*>(a.c_str()));
  argv.push_back(nullptr);
  const pid_t pid = fork();
  if (pid < 0) return -1;
  if (pid == 0) {
    execvp(argv[0], argv.data());
    _exit(127);
  }
  int st = 0;
  waitpid(pid, &st, 0);
  return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}

std::string ip_tool() { return access("/usr/sbin/ip", X_OK) == 0 ? "/usr/sbin/ip" : "ip"; }

bool constant_time_eq(const std::string& a, const std::string& b) {
  if (a.size() != b.size()) return false;
  unsigned char d = 0;
  for (std::size_t i = 0; i < a.size(); ++i) d |= static_cast<unsigned char>(a[i] ^ b[i]);
  return d == 0;
}

struct Agent {
  std::string version, node_name, token, default_iface;
  std::mutex m;
  std::unique_ptr<Run> run;
  std::string state = "idle";          // idle | loaded | running | finished
  std::vector<std::string> addresses;  // /32s we added (removed on DELETE)
  std::string iface;
  std::atomic<std::int64_t> last_seen_ns{0};

  void remove_addresses() {
    for (const auto& a : addresses) run_cmd({ip_tool(), "addr", "del", a + "/32", "dev", iface});
    addresses.clear();
  }
};

void reply(httplib::Response& res, int status, const json& body) {
  res.status = status;
  res.set_content(body.dump(), "application/json");
}

}  // namespace

int serve_main(const std::string& listen, const std::string& token_file, const std::string& default_iface,
               const std::string& version) {
  Agent ag;
  ag.version = version;
  ag.default_iface = default_iface;
  {
    std::ifstream f(token_file);
    std::getline(f, ag.token);
    while (!ag.token.empty() && (ag.token.back() == '\n' || ag.token.back() == '\r' || ag.token.back() == ' '))
      ag.token.pop_back();
    if (ag.token.size() < 16) {
      std::fprintf(stderr, "token file %s missing or too short (need >= 16 characters)\n", token_file.c_str());
      return 2;
    }
  }
  char hn[256] = {};
  gethostname(hn, sizeof(hn) - 1);
  ag.node_name = hn;
  const auto colon = listen.rfind(':');
  if (colon == std::string::npos) {
    std::fprintf(stderr, "--listen needs ADDRESS:PORT\n");
    return 2;
  }
  const std::string host = listen.substr(0, colon);
  const int port = std::stoi(listen.substr(colon + 1));

  httplib::Server svr;
  svr.set_payload_max_length(512ull << 20);  // plans with ~1 M prefixes
  svr.set_pre_routing_handler([&](const httplib::Request& req, httplib::Response& res) {
    const auto auth = req.get_header_value("Authorization");
    if (!constant_time_eq(auth, "Bearer " + ag.token)) {
      reply(res, 401, {{"error", "missing or wrong token"}});
      return httplib::Server::HandlerResponse::Handled;
    }
    ag.last_seen_ns = now_ns();
    return httplib::Server::HandlerResponse::Unhandled;
  });

  svr.Get("/v1/status", [&](const httplib::Request&, httplib::Response& res) {
    std::lock_guard lk(ag.m);
    json by_state = json::object();
    if (ag.run && ag.state != "idle") {
      try { by_state = ag.run->stats()["by_state"]; } catch (const std::exception&) {}
    }
    reply(res, 200, {{"version", ag.version}, {"node", ag.node_name}, {"state", ag.state},
                     {"run_id", ag.run ? ag.run->plan().run_id : ""}, {"now_ms", now_ns() / 1'000'000},
                     {"routers", ag.run ? ag.run->routers() : 0}, {"sessions", by_state},
                     {"cores", std::thread::hardware_concurrency()}});
  });

  svr.Put("/v1/run", [&](const httplib::Request& req, httplib::Response& res) {
    std::lock_guard lk(ag.m);
    if (ag.run) return reply(res, 409, {{"error", "a run is loaded (" + ag.state + "); DELETE /v1/run first"}});
    Plan plan;
    try {
      plan = plan_from_json(json::parse(req.body));
    } catch (const std::exception& e) {
      return reply(res, 400, {{"error", std::string("plan: ") + e.what()}});
    }
    if (plan.timing.present) return reply(res, 400, {{"error", "plan: serve mode takes phases over the API, not \"timing\""}});
    ag.iface = plan.iface.empty() ? ag.default_iface : plan.iface;
    if (!ag.iface.empty()) {
      for (const auto& r : plan.routers) {
        const auto ip = bgp::format_ipv4(r.ip);
        if (run_cmd({ip_tool(), "addr", "replace", ip + "/32", "dev", ag.iface}) != 0) {
          ag.remove_addresses();
          return reply(res, 400, {{"error", "cannot add " + ip + "/32 on " + ag.iface + " (CAP_NET_ADMIN?)"}});
        }
        ag.addresses.push_back(ip);
      }
    }
    if (!ag.iface.empty() && !plan.routers.empty()) {
      // the DUT may still map these addresses to the node that had them in an earlier run
      std::vector<std::uint32_t> ips;
      for (const auto& r : plan.routers) ips.push_back(r.ip);
      std::string err;
      if (send_garp(ag.iface, ips, err) < 0) std::fprintf(stderr, "warning: gratuitous ARP: %s\n", err.c_str());
    }
    const auto n = plan.routers.size();
    ag.run = std::make_unique<Run>(std::move(plan), ag.version);
    ag.state = "loaded";
    std::fprintf(stderr, "run %s loaded: %zu routers\n", ag.run->plan().run_id.c_str(), n);
    reply(res, 200, {{"routers", n}, {"addresses", ag.addresses.size()}});
  });

  svr.Post("/v1/run/phase", [&](const httplib::Request& req, httplib::Response& res) {
    std::lock_guard lk(ag.m);
    if (!ag.run || ag.state == "finished") return reply(res, 409, {{"error", "no run loaded or run finished"}});
    try {
      const json j = json::parse(req.body);
      const PhaseCmd c = phase_from_json(j);
      if (c.type == PhaseType::Flap) {
        const auto names = j.value("params", json::object()).value("routers", std::vector<std::string>{});
        if (names.empty()) return reply(res, 400, {{"error", "flap: params.routers is empty"}});
        ag.run->schedule_for(c, names);
      } else {
        ag.run->schedule(c);
      }
      ag.state = "running";
      reply(res, 202, {{"type", j.value("type", "")}, {"at_ms", j.value("at_ms", 0LL)}});
    } catch (const std::exception& e) {
      reply(res, 400, {{"error", e.what()}});
    }
  });

  svr.Get("/v1/run/stats", [&](const httplib::Request&, httplib::Response& res) {
    std::lock_guard lk(ag.m);
    if (!ag.run) return reply(res, 409, {{"error", "no run loaded"}});
    try {
      json s = ag.run->stats();
      if (ag.state == "running" && ag.run->all_stopped()) ag.state = "finished";
      s["state"] = ag.state;
      reply(res, 200, s);
    } catch (const std::exception& e) {
      reply(res, 500, {{"error", e.what()}});
    }
  });

  svr.Post("/v1/run/stop", [&](const httplib::Request&, httplib::Response& res) {
    std::lock_guard lk(ag.m);
    if (!ag.run) return reply(res, 409, {{"error", "no run loaded"}});
    ag.run->stop();
    ag.state = "finished";
    std::fprintf(stderr, "run stopped by the controller\n");
    reply(res, 200, {{"state", ag.state}});
  });

  svr.Get("/v1/run/result", [&](const httplib::Request&, httplib::Response& res) {
    std::lock_guard lk(ag.m);
    if (!ag.run) return reply(res, 409, {{"error", "no run loaded"}});
    if (ag.state != "finished" && !ag.run->all_stopped())
      return reply(res, 409, {{"error", "run not finished (state " + ag.state + ")"}});
    ag.state = "finished";
    reply(res, 200, ag.run->result());
  });

  svr.Delete("/v1/run", [&](const httplib::Request&, httplib::Response& res) {
    std::lock_guard lk(ag.m);
    if (ag.run) {
      ag.run.reset();  // stops routers, joins threads
      std::fprintf(stderr, "run deleted\n");
    }
    ag.remove_addresses();
    ag.state = "idle";
    reply(res, 200, {{"state", ag.state}});
  });

  // watchdog: a running run without controller contact stops by itself (spec §10)
  std::atomic<bool> quit{false};
  std::thread watchdog([&] {
    while (!quit) {
      std::this_thread::sleep_for(std::chrono::milliseconds(250));
      std::lock_guard lk(ag.m);
      if (!ag.run || ag.state != "running") continue;
      const double idle_s = static_cast<double>(now_ns() - ag.last_seen_ns) / 1e9;
      if (idle_s > ag.run->plan().controller_timeout_s) {
        std::fprintf(stderr, "watchdog: no contact with the controller for %.1f s - stopping the run\n", idle_s);
        try { ag.run->stop(); } catch (...) {}
        ag.state = "finished";
      }
    }
  });

  std::fprintf(stderr, "tgen-agent %s serving on %s (node %s)\n", version.c_str(), listen.c_str(), ag.node_name.c_str());
  const bool ok = svr.listen(host, port);
  quit = true;
  watchdog.join();
  if (!ok) {
    std::fprintf(stderr, "cannot listen on %s\n", listen.c_str());
    return 1;
  }
  return 0;
}

}  // namespace tgen
