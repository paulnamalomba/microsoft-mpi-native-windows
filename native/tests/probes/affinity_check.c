/* Reports each rank's applied affinity: default CPU Sets (Windows 11 / Server 2022+),
 * process affinity mask, process groups and the processors it was observed running on.
 * Rank 0 prints one AFFINITY line per rank; native/verify.py compares them with the
 * layout mpiexec computed (MPIEXEC_AFFINITY_TABLE=1). */
#include <stdio.h>
#include <windows.h>
#include <mpi.h>

typedef BOOL (WINAPI *GetDefaultCpuSetMasks)(HANDLE, PGROUP_AFFINITY, USHORT, PUSHORT);

typedef struct {
    int rank;
    int cpuset_count;          /* -1: API unavailable */
    unsigned int cpuset_group;
    unsigned long long cpuset_mask;
    unsigned long long process_mask;
    unsigned int process_groups;
    unsigned int observed_group;
    unsigned int observed_other_groups;
    unsigned long long observed_mask;
} Report;

int main(int argc, char **argv)
{
    Report mine = {0}, *all = NULL;
    int rank = 0, size = 0, i;
    GROUP_AFFINITY masks[4];
    USHORT required = 0, groups[4], group_count = 4;
    DWORD_PTR process_mask = 0, system_mask = 0;
    PROCESSOR_NUMBER number;
    GetDefaultCpuSetMasks get_masks =
        (GetDefaultCpuSetMasks)GetProcAddress(GetModuleHandleW(L"kernelbase"), "GetProcessDefaultCpuSetMasks");

    MPI_Init(&argc, &argv);
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &size);
    mine.rank = rank;
    mine.cpuset_count = -1;
    if (get_masks != NULL && get_masks(GetCurrentProcess(), masks, 4, &required)) {
        mine.cpuset_count = required;
        if (required > 0) {
            mine.cpuset_group = masks[0].Group;
            mine.cpuset_mask = masks[0].Mask;
        }
    }
    if (GetProcessAffinityMask(GetCurrentProcess(), &process_mask, &system_mask)) {
        mine.process_mask = process_mask;
    }
    if (GetProcessGroupAffinity(GetCurrentProcess(), &group_count, groups)) {
        mine.process_groups = group_count;
    }
    GetCurrentProcessorNumberEx(&number);
    mine.observed_group = number.Group;
    for (i = 0; i < 4000; ++i) {
        GetCurrentProcessorNumberEx(&number);
        if (number.Group == mine.observed_group) {
            mine.observed_mask |= 1ULL << number.Number;
        } else {
            mine.observed_other_groups = 1;
        }
        if (i % 50 == 0) {
            SwitchToThread();
        }
    }
    if (rank == 0) {
        all = (Report *)calloc(size, sizeof(Report));
    }
    MPI_Gather(&mine, (int)sizeof(Report), MPI_BYTE, all, (int)sizeof(Report), MPI_BYTE, 0, MPI_COMM_WORLD);
    if (rank == 0) {
        for (i = 0; i < size; ++i) {
            printf("AFFINITY rank=%d cpusets=%d cpuset=%u:0x%llx process=0x%llx groups=%u observed=%u:0x%llx other_groups=%u\n",
                   all[i].rank, all[i].cpuset_count, all[i].cpuset_group, all[i].cpuset_mask, all[i].process_mask,
                   all[i].process_groups, all[i].observed_group, all[i].observed_mask, all[i].observed_other_groups);
        }
        printf("AFFINITY_DONE ranks=%d\n", size);
        free(all);
    }
    MPI_Finalize();
    return 0;
}
