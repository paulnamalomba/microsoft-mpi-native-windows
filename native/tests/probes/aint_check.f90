! Positive check of the Fortran MPI_AINT_ADD/MPI_AINT_DIFF bindings through msmpifec.lib aliases.
program aint_check
    implicit none
    include 'mpif.h'
    integer(kind=MPI_ADDRESS_KIND), external :: MPI_AINT_ADD, MPI_AINT_DIFF
    integer(kind=MPI_ADDRESS_KIND) :: first, last, total_size
    double precision :: values(8)
    integer :: rank, size, ierr, local, total

    call MPI_INIT(ierr)
    call MPI_COMM_RANK(MPI_COMM_WORLD, rank, ierr)
    call MPI_COMM_SIZE(MPI_COMM_WORLD, size, ierr)
    call MPI_GET_ADDRESS(values(1), first, ierr)
    call MPI_GET_ADDRESS(values(8), last, ierr)
    total_size = 7 * 8
    local = 0
    if (MPI_AINT_ADD(first, total_size) == last .and. MPI_AINT_DIFF(last, first) == total_size) local = 1
    call MPI_ALLREDUCE(local, total, 1, MPI_INTEGER, MPI_SUM, MPI_COMM_WORLD, ierr)
    if (rank == 0) then
        if (total == size) then
            print '(A,I0,A,I0,A)', 'AINT_FORTRAN ranks=', size, ' passed=', total, ' PASS'
        else
            print '(A,I0,A,I0,A)', 'AINT_FORTRAN ranks=', size, ' passed=', total, ' FAIL'
        end if
    end if
    call MPI_FINALIZE(ierr)
end program aint_check
