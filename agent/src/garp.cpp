// SPDX-License-Identifier: MIT
// Gratuitous ARP for the emulated routers' addresses.
//
// Router addresses move between nodes from one run to the next (the plan spreads them
// by router count). A DUT that keeps old ARP entries (Cisco IOS: 4 hours) then answers
// a router's TCP SYN to the node that had the address before, and the session never
// comes up (seen on the 1841: 7 of 12, ASR-920: 20 of 30). Announcing every address
// right after it is added updates the DUT's ARP cache at once.
#include "tgen/garp.hpp"

#include <arpa/inet.h>
#include <linux/if_packet.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstring>

namespace tgen {

int send_garp(const std::string& iface, const std::vector<std::uint32_t>& ips_host_order, std::string& err,
              int repeat) {
  if (iface.empty() || iface.size() >= IFNAMSIZ) {
    err = "invalid interface name";
    return -1;
  }
  const int fd = ::socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ARP));
  if (fd < 0) {
    err = std::string("raw socket: ") + std::strerror(errno) + " (needs CAP_NET_RAW)";
    return -1;
  }
  ifreq ifr{};
  std::strncpy(ifr.ifr_name, iface.c_str(), IFNAMSIZ - 1);
  if (::ioctl(fd, SIOCGIFINDEX, &ifr) < 0) {
    err = "interface " + iface + ": " + std::strerror(errno);
    ::close(fd);
    return -1;
  }
  const int ifindex = ifr.ifr_ifindex;
  if (::ioctl(fd, SIOCGIFHWADDR, &ifr) < 0 || ifr.ifr_hwaddr.sa_family != 1 /* ARPHRD_ETHER */) {
    err = "interface " + iface + " has no Ethernet address";
    ::close(fd);
    return -1;
  }
  std::array<std::uint8_t, 6> mac{};
  std::memcpy(mac.data(), ifr.ifr_hwaddr.sa_data, 6);

  sockaddr_ll to{};
  to.sll_family = AF_PACKET;
  to.sll_protocol = htons(ETH_P_ARP);
  to.sll_ifindex = ifindex;
  to.sll_halen = 6;
  std::memset(to.sll_addr, 0xff, 6);

  int sent = 0;
  for (int round = 0; round < repeat; ++round) {
    for (const auto ip_h : ips_host_order) {
      const std::uint32_t ip = htonl(ip_h);
      // both forms: some stacks only refresh on a request, others only on a reply
      for (const std::uint16_t op : {1, 2}) {
        std::array<std::uint8_t, 42> f{};
        std::memset(f.data(), 0xff, 6);                       // dst: broadcast
        std::memcpy(f.data() + 6, mac.data(), 6);             // src
        f[12] = 0x08; f[13] = 0x06;                           // ethertype ARP
        f[14] = 0x00; f[15] = 0x01;                           // htype Ethernet
        f[16] = 0x08; f[17] = 0x00;                           // ptype IPv4
        f[18] = 6; f[19] = 4;
        f[20] = 0; f[21] = static_cast<std::uint8_t>(op);
        std::memcpy(f.data() + 22, mac.data(), 6);            // sender MAC
        std::memcpy(f.data() + 28, &ip, 4);                   // sender IP
        if (op == 2) std::memset(f.data() + 32, 0xff, 6);     // target MAC (reply: broadcast)
        std::memcpy(f.data() + 38, &ip, 4);                   // target IP = sender IP
        if (::sendto(fd, f.data(), f.size(), 0, reinterpret_cast<sockaddr*>(&to), sizeof(to)) == 42) ++sent;
      }
    }
    if (round + 1 < repeat) ::usleep(200'000);
  }
  ::close(fd);
  return sent;
}

}  // namespace tgen
