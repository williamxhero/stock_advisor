using Xunit;

// WPF Application.Current and input/presentation state are process-global even
// when each test owns an STA thread. Keep window-based desktop contracts from
// overlapping in the same test host.
[assembly: CollectionBehavior(DisableTestParallelization = true)]
