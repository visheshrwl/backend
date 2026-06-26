# cgroup-v2

cgroups are a kernel mechanism by which you can isolate and control things like memory and CPU IO, you have probably seen this if you have used containerisation.

## Timeline of modern resource control

- OpenVZ (2005)
- cgroup v1 (2007)
- cgroup v2 (2016)
- Real CPU control (2017)
- PSI (2018)
- Senpai (2019)
- io.latency (2019)
- 


If you have run a kernel before 5.8, you have a drastically less useful resource swap algorithms, You're also missing for a huge number of containerisation improvements which you get for the free even if you just continued running in the old container but with new kernel.

You probably have used cgroups only through your service manager like systemd >= 226 and containerd >= 1.4, podman > 1.4.4

Most distributions boot with cgroup v1 while they supports v2, but by default only open v1.

cgroup v1 by default has hierarchy per resource


