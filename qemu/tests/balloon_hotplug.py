import random
import re
import time

from virttest import error_context, utils_test
from virttest.qemu_devices import qdevices

from provider import win_driver_utils
from qemu.tests.balloon_check import BallooningTestLinux, BallooningTestWin


@error_context.context_aware
def run(test, params, env):
    """
    Test hotplug of balloon devices.

    1) Boot up guest w/o balloon device.
    2) Hoplug balloon device and check hotplug successfully or not.
    3) Install balloon service and check its status in windows guests.
    4) Do memory balloon.
    5) Reboot/shutdown guest after hotplug balloon device(option)
    6) Do memory balloon after guest reboot(option)
    7) Unplug balloon device and check unplug successfully or not.
    8) Reboot/shutdown guest after unplug balloon device(option)

    :param test:   QEMU test object.
    :param params: Dictionary with the test parameters.
    :param env:    Dictionary with test environment.
    """

    def run_pm_test(pm_test, plug_type):
        """
        Run pm(reboot/system_reset/shutdown) related test after balloon
        device is hot-plug or hot-unplug
        :param pm_test: power management test name,e.g. reboot/shutdown
        :param plug_type:balloon device plug operation,e.g.hot_plug or hot_unplug
        """
        error_context.context(
            "Run %s test after %s balloon device" % (pm_test, plug_type), test.log.info
        )
        utils_test.run_virt_sub_test(test, params, env, pm_test)

    def enable_balloon_service():
        """
        Install balloon service and check its status in windows guests
        """
        if params["os_type"] != "windows":
            return
        error_context.context(
            "Install and check balloon service in windows guest", test.log.info
        )
        session = vm.wait_for_login()
        driver_name = params.get("driver_name", "balloon")
        session = utils_test.qemu.windrv_check_running_verifier(
            session, vm, test, driver_name
        )
        balloon_test.configure_balloon_service(session)

        output = balloon_test.operate_balloon_service(session, "status")
        if not re.search(r"running", output.lower(), re.M):
            test.error("Ballooon service status is not running")
        session.close()

    pm_test_after_plug = params.get("pm_test_after_plug")
    pm_test_after_unplug = params.get("pm_test_after_unplug")
    unplug_timeout = params.get("unplug_timeout", 30)
    vm = env.get_vm(params["main_vm"])
    vm.verify_alive()

    balloon_device = params.get("balloon_device", "virtio-balloon-pci")
    balloon_id = "balloon0"  # Reuse same ID - tests proper cleanup after unplug
    # Updated with enhanced DEBUG logging - 2026-10-16

    error_context.context("Hotplug and unplug balloon device in a loop", test.log.info)
    for i in range(int(params.get("balloon_repeats", 3))):
        test.log.info("DEBUG: ========== Starting iteration %d ==========" % i)
        all_balloons_before = vm.devices.get_by_params({"driver": "virtio-balloon-pci"})
        test.log.info("DEBUG: Balloon devices at start of iteration %d: %s" %
                     (i, [dev.get_aid() for dev in all_balloons_before]))

        vm.devices.set_dirty()
        test.log.info("DEBUG: Called vm.devices.set_dirty()")

        new_dev = qdevices.QDevice(
            balloon_device,
            {"id": balloon_id},
            parent_bus={"aobject": params.get("balloon_bus", "pci.0")},
        )
        test.log.info("DEBUG: Created QDevice with id='%s'" % balloon_id)

        error_context.context(
            "Hotplug balloon device for %d times" % (i + 1), test.log.info
        )
        out = vm.devices.simple_hotplug(new_dev, vm.monitor)
        if out[1] is False:
            test.log.error("DEBUG: Hotplug failed! out[0]=%s" % out[0])
            test.fail("Failed to hotplug balloon in iteration %s, %s" % (i, out[0]))
        test.log.info("DEBUG: Hotplug succeeded, out=%s" % str(out))

        # temporary workaround for migration
        vm.params["balloon"] = balloon_id
        vm.params["balloon_dev_devid"] = balloon_id
        vm.params["balloon_dev_add_bus"] = "yes"
        devs = vm.devices.get_by_params({"id": balloon_id})
        test.log.info("DEBUG: After hotplug, looking for '%s', found %d devices" % (balloon_id, len(devs)))
        if devs:
            test.log.info("DEBUG: Device aid=%s, qid=%s" % (devs[0].get_aid(), devs[0].get_qid()))
        else:
            test.log.error("DEBUG: ERROR - get_by_params returned empty list!")
            all_devs = vm.devices.get_by_params({})
            test.log.error("DEBUG: All devices: %s" % [dev.get_aid() for dev in all_devs[:20]])
        vm.params["balloon_pci_bus"] = devs[0]["bus"]

        if params["os_type"] == "windows":
            balloon_test = BallooningTestWin(test, params, env)
        else:
            balloon_test = BallooningTestLinux(test, params, env)
        min_sz, max_sz = balloon_test.get_memory_boundary()

        enable_balloon_service()

        error_context.context(
            "Check whether balloon device work after hotplug", test.log.info
        )
        balloon_test.balloon_memory(int(random.uniform(min_sz, max_sz)))

        if pm_test_after_plug:
            run_pm_test(pm_test_after_plug, "hot-plug")
            # run balloon test after reboot,skip followed test if
            # pm_test_after_plug is shutdown
            if vm.is_alive():
                balloon_test.balloon_memory(int(random.uniform(min_sz, max_sz)))
            else:
                return

        if params["os_type"] == "windows":
            time.sleep(10)

        error_context.context(
            "Unplug balloon device for %d times" % (i + 1), test.log.info
        )

        # Verify device exists before unplugging
        test.log.info("DEBUG: About to unplug device with id=%s" % balloon_id)
        if devs:
            test.log.info("DEBUG: devs[0].get_aid()=%s, devs[0].get_qid()=%s" %
                         (devs[0].get_aid(), devs[0].get_qid()))

        # Use the device ID directly for clarity
        out = vm.devices.simple_unplug(
            balloon_id, vm.monitor, timeout=unplug_timeout
        )
        if out[1] is False:
            test.log.error("DEBUG: Unplug FAILED! out=%s" % str(out))
            all_balloons = vm.devices.get_by_params({"driver": "virtio-balloon-pci"})
            test.log.error("DEBUG: All balloon devices in vm.devices after failed unplug: %s" %
                          [dev.get_aid() for dev in all_balloons])
            test.fail("Failed to unplug balloon in iteration %s, %s" % (i, out[0]))

        test.log.info("DEBUG: Unplug succeeded! out=%s" % str(out))

        # Wait and verify device is fully removed before continuing
        # Heavy memory deflation can cause slow device release
        test.log.info("DEBUG: Waiting for device cleanup to complete...")
        time.sleep(5)

        # Force device container refresh to see actual state
        vm.devices.set_dirty()
        remaining_balloons = vm.devices.get_by_params({"driver": "virtio-balloon-pci"})
        test.log.info("DEBUG: After unplug + set_dirty, remaining balloon devices: %s" %
                     [dev.get_aid() for dev in remaining_balloons])

        # Wait up to 30s for device to fully disappear
        wait_time = 0
        while remaining_balloons and wait_time < 30:
            test.log.warning("DEBUG: Device still present after %ds, waiting..." % wait_time)
            time.sleep(2)
            wait_time += 2
            vm.devices.set_dirty()
            remaining_balloons = vm.devices.get_by_params({"driver": "virtio-balloon-pci"})

        if remaining_balloons:
            test.log.error("DEBUG: ERROR - Devices still present after %ds: %s" %
                          (wait_time, [dev.get_aid() for dev in remaining_balloons]))
            test.fail("Device cleanup failed - balloon device still present after unplug")
        else:
            test.log.info("DEBUG: Device successfully removed after %ds" % (wait_time + 5))

        if params.get("migrate_after_unplug", "no") == "yes":
            error_context.context(
                "Migrate after hotunplug balloon device", test.log.info
            )
            # temporary workaround for migration
            del vm.params["balloon"]
            del vm.params["balloon_dev_devid"]
            del vm.params["balloon_dev_add_bus"]
            del vm.params["balloon_pci_bus"]
            vm.migrate(float(params.get("mig_timeout", "3600")))

        if pm_test_after_unplug:
            run_pm_test(pm_test_after_unplug, "hot-unplug")
            if not vm.is_alive():
                return

    # for windows guest, disable/uninstall driver to get memory leak based on
    # driver verifier is enabled
    if params.get("os_type") == "windows":
        test.log.info("DEBUG: Windows final hotplug, reusing balloon0")
        vm.devices.set_dirty()
        final_dev = qdevices.QDevice(
            balloon_device,
            {"id": balloon_id},  # Reuse balloon0
            parent_bus={"aobject": params.get("balloon_bus", "pci.0")},
        )
        out = vm.devices.simple_hotplug(final_dev, vm.monitor)
        if out[1] is False:
            test.fail("Failed to hotplug balloon at last, output is %s" % out[0])
        win_driver_utils.memory_leak_check(vm, test, params)
    error_context.context("Verify guest alive!", test.log.info)
    vm.verify_kernel_crash()
