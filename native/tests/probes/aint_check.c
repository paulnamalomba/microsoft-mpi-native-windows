/* Positive check of MPI_Aint_add/MPI_Aint_diff (MPI-3.1) against MPI_Get_address. */
#include <stdio.h>
#include <windows.h>
#include <mpi.h>

int main(int argc, char **argv)
{
    double values[8];
    char version[MPI_MAX_LIBRARY_VERSION_STRING];
    wchar_t path[MAX_PATH];
    MPI_Aint first = 0, last = 0, sum = 0, difference = 0, local = 0, total = 0;
    int rank = 0, size = 0, length = 0;

    MPI_Init(&argc, &argv);
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &size);
    MPI_Get_address(&values[0], &first);
    MPI_Get_address(&values[7], &last);
    sum = MPI_Aint_add(first, 7 * (MPI_Aint)sizeof(double));
    difference = MPI_Aint_diff(last, first);
    local = (sum == last && difference == 7 * (MPI_Aint)sizeof(double)
             && PMPI_Aint_add(first, difference) == last && PMPI_Aint_diff(first, last) == -difference) ? 1 : 0;
    MPI_Allreduce(&local, &total, 1, MPI_AINT, MPI_SUM, MPI_COMM_WORLD);
    MPI_Get_library_version(version, &length);
    GetModuleFileNameW(GetModuleHandleW(L"msmpi.dll"), path, MAX_PATH);
    if (rank == 0) {
        printf("AINT_C ranks=%d passed=%lld library=[%s] dll=[%ls] %s\n", size, (long long)total, version, path,
               total == size ? "PASS" : "FAIL");
    }
    MPI_Finalize();
    return total == size ? 0 : 1;
}
