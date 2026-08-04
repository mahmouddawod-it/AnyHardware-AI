from ssd_llm.planner import GiB, Machine, plan_run


def test_low_memory_plan_keeps_cpu_only_and_small_context():
    plan = plan_run(Machine(logical_cpus=4, available_ram_bytes=3 * GiB))
    assert plan.gpu_layers == 0
    assert plan.mmap is True
    assert plan.threads == 3
    assert plan.context == 512
    assert plan.batch == 512


def test_user_overrides_are_preserved():
    plan = plan_run(Machine(logical_cpus=16, available_ram_bytes=16 * GiB),
                    threads=8, context=8192, batch=512)
    assert (plan.threads, plan.context, plan.batch) == (8, 8192, 512)
