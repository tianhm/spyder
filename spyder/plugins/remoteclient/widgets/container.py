# -*- coding: utf-8 -*-
#
# Copyright © Spyder Project Contributors
#
# Licensed under the terms of the MIT License
# (see spyder/__init__.py for details)

"""Remote client container."""

# Standard library imports
from __future__ import annotations
from collections import deque
import functools

# Third-party imports
from qtpy.QtCore import Signal
from qtpy.QtWidgets import QMessageBox

# Local imports
from spyder.api.asyncdispatcher import AsyncDispatcher
from spyder.api.translations import _
from spyder.api.widgets.main_container import PluginMainContainer
from spyder.plugins.ipythonconsole.utils.kernel_handler import KernelHandler
from spyder.plugins.remoteclient import SPYDER_REMOTE_MAX_VERSION
from spyder.plugins.remoteclient.api import (
    MAX_CLIENT_MESSAGES,
    RemoteClientActions,
    RemoteClientMenus,
    RemoteConsolesMenuSections,
)
from spyder.plugins.remoteclient.api.protocol import ConnectionInfo
from spyder.plugins.remoteclient.widgets.connectiondialog import (
    ConnectionDialog,
)


class RemoteClientContainer(PluginMainContainer):
    sig_start_server_requested = Signal(str)
    """
    This signal is used to request starting a remote server.

    Parameters
    ----------
    id: str
        Id of the server that will be started.
    """

    sig_stop_server_requested = Signal(str)
    """
    This signal is used to request stopping a remote server.

    Parameters
    ----------
    id: str
        Id of the server that will be stopped.
    """

    sig_server_renamed = Signal(str)
    """
    This signal is used to inform that a remote server was renamed.

    Parameters
    ----------
    id: str
        Id of the server that was renamed.
    """

    sig_connection_status_changed = Signal(dict)
    """
    This signal is used to update the status of a given connection.

    Parameters
    ----------
    info: ConnectionInfo
        Dictionary with the necessary info to update the status of a
        connection.
    """

    sig_create_ipyclient_requested = Signal(str)
    """
    This signal is used to request starting an IPython console client for a
    remote server.

    Parameters
    ----------
    id: str
        Id of the server for which a client will be created.
    """

    sig_shutdown_kernel_requested = Signal(str, str)
    """
    This signal is used to request shutting down a kernel.

    Parameters
    ----------
    id: str
        Id of the server for which a kernel shutdown will be requested.
    kernel_id: str
        Id of the kernel which will be shutdown in the server.
    """

    sig_interrupt_kernel_requested = Signal(str, str)
    """
    This signal is used to request interrupting a kernel.

    Parameters
    ----------
    id: str
        Id of the server for which a kernel interrupt will be requested.
    kernel_id: str
        Id of the kernel which will be shutdown in the server.
    """

    sig_client_message_logged = Signal(dict)
    """
    This signal is used to inform that a client has logged a connection
    message.

    Parameters
    ----------
    log: RemoteClientLog
        Dictionary that contains the log message and its metadata.
    """

    # ---- PluginMainContainer API
    # -------------------------------------------------------------------------
    def setup(self):
        # Attributes
        self.client_logs: dict[str, deque] = {}

        # Widgets
        self.create_action(
            RemoteClientActions.ManageConnections,
            _("Manage remote connections..."),
            icon=self._plugin.get_icon(),
            triggered=self._show_connection_dialog,
        )

        self._remote_consoles_menu = self.create_menu(
            RemoteClientMenus.RemoteConsoles, _("New console in remote server")
        )

        # Signals
        self.sig_connection_status_changed.connect(
            self._on_connection_status_changed
        )
        self.sig_client_message_logged.connect(self._on_client_message_logged)

        self.__requested_restart = False
        self.__requested_info = False

    def update_actions(self):
        pass

    # ---- Public API
    # -------------------------------------------------------------------------
    def setup_remote_consoles_submenu(self, render=True):
        """Create the remote consoles submenu in the Consoles app one."""
        self._remote_consoles_menu.clear_actions()

        self.add_item_to_menu(
            self.get_action(RemoteClientActions.ManageConnections),
            menu=self._remote_consoles_menu,
            section=RemoteConsolesMenuSections.ManagerSection,
        )

        servers = self.get_conf("servers", default={})
        for config_id in servers:
            auth_method = self.get_conf(f"{config_id}/auth_method")
            name = self.get_conf(f"{config_id}/{auth_method}/name")

            action = self.create_action(
                name=config_id,
                text=f"New console in {name} server",
                icon=self.create_icon("ipython_console"),
                triggered=functools.partial(
                    self.sig_create_ipyclient_requested.emit,
                    config_id,
                ),
                overwrite=True,
            )
            self.add_item_to_menu(
                action,
                menu=self._remote_consoles_menu,
                section=RemoteConsolesMenuSections.ConsolesSection,
            )

        # This is necessary to reposition the menu correctly when rebuilt
        if render:
            self._remote_consoles_menu.render()

    def on_kernel_started(self, ipyclient, kernel_info):
        """
        Actions to take when a remote kernel was started for an IPython console
        client.
        """
        config_id = ipyclient.server_id

        # It's only at this point that we can allow users to close the client.
        ipyclient.can_close = True

        # Handle failures to launch a kernel
        if not kernel_info:
            auth_method = self.get_conf(f"{config_id}/auth_method")
            name = self.get_conf(f"{config_id}/{auth_method}/name")
            ipyclient.show_kernel_error(
                _(
                    "There was an error connecting to the server <b>{}</b>. "
                    "Please check your connection is working."
                ).format(name)
            )
            return

        # Connect client's signals
        ipyclient.kernel_id = kernel_info["id"]
        self._connect_ipyclient_signals(ipyclient)

        try:
            kernel_handler = KernelHandler.from_connection_info(
                kernel_info["connection_info"],
                ssh_connection=self._plugin.get_remote_server(
                    ipyclient.server_id
                )._ssh_connection,
            )

            # Need to be smaller than the usual time it takes for the kernel to
            # restart
            kernel_handler.set_time_to_dead(1.0)
        except Exception as err:
            ipyclient.show_kernel_error(err)
        else:
            # Connect client to the kernel
            ipyclient.connect_kernel(kernel_handler)

    def on_server_version_mismatch(self, config_id, version: str):
        """
        Actions to take when there's a mismatch between the
        spyder-remote-services version installed in the server and the one
        supported by Spyder.
        """
        auth_method = self.get_conf(f"{config_id}/auth_method")
        server_name = self.get_conf(f"{config_id}/{auth_method}/name")

        QMessageBox.critical(
            self,
            _("Remote server error"),
            _(
                "The version of <tt>spyder-remote-services</tt> on the "
                "remote host <b>{server}</b> (<b>{srs_version}</b>) is newer "
                "than the latest Spyder supports (<b>{max_version}</b>)."
                "<br><br>"
                "Please update Spyder to be able to connect to this host."
            ).format(
                server=server_name,
                srs_version=version,
                max_version=SPYDER_REMOTE_MAX_VERSION,
            ),
            QMessageBox.Ok,
        )

    # ---- Private API
    # -------------------------------------------------------------------------
    def _show_connection_dialog(self):
        connection_dialog = ConnectionDialog(self)

        connection_dialog.sig_start_server_requested.connect(
            self.sig_start_server_requested
        )
        connection_dialog.sig_stop_server_requested.connect(
            self.sig_stop_server_requested
        )
        connection_dialog.sig_connections_changed.connect(
            self.setup_remote_consoles_submenu
        )
        connection_dialog.sig_server_renamed.connect(self.sig_server_renamed)

        connection_dialog.show()

    def _on_connection_status_changed(self, info: ConnectionInfo):
        """Handle changes in connection status."""
        host_id = info["id"]
        status = info["status"]
        message = info["message"]

        # We need to save this info so that we can show the current status in
        # the connection dialog when it's closed and opened again.
        self.set_conf(f"{host_id}/status", status)
        self.set_conf(f"{host_id}/status_message", message)

    def _connect_ipyclient_signals(self, ipyclient):
        """
        Connect the signals to shutdown, interrupt and restart the kernel of an
        IPython console client to the signals and methods declared here for
        remote kernel management.
        """
        ipyclient.sig_shutdown_kernel_requested.connect(
            self.sig_shutdown_kernel_requested
        )
        ipyclient.sig_interrupt_kernel_requested.connect(
            self.sig_interrupt_kernel_requested
        )
        ipyclient.sig_restart_kernel_requested.connect(
            lambda: self._request_kernel_restart(ipyclient)
        )
        ipyclient.sig_kernel_died.connect(
            lambda: self._request_kernel_info(ipyclient)
        )

    def _request_kernel_restart(self, ipyclient):
        """
        Request a kernel restart to the server for an IPython console client
        and handle its response.
        """
        if self.__requested_restart:
            return

        future = self._plugin._restart_kernel(
            ipyclient.server_id, ipyclient.kernel_id
        )

        self.__requested_restart = True

        future.connect(
            AsyncDispatcher.QtSlot(lambda future: self._on_kernel_restarted(
                ipyclient, future.result()
            ))
        )

    def _request_kernel_info(self, ipyclient):
        """
        Request a kernel reconnect to the server for an IPython console client
        and handle its response.
        """
        if self.__requested_info:
            return

        future = self._plugin._get_kernel_info(
            ipyclient.server_id, ipyclient.kernel_id
        )

        self.__requested_info = True

        future.connect(
            AsyncDispatcher.QtSlot(lambda future: self._on_kernel_info_reply(
                ipyclient, future.result()
            ))
        )

    def _on_kernel_restarted(self, ipyclient, restarted: bool):
        """
        Get kernel info corresponding to an IPython console client from the
        server.

        If we get a response, it means the kernel is alive.
        """
        self.__requested_restart = False
        if restarted:
            ipyclient.kernel_handler.reconnect_kernel()
            ipyclient.handle_remote_kernel_restarted()
        else:
            ipyclient.remote_kernel_restarted_failure_message(shutdown=True)

    def _on_kernel_info_reply(self, ipyclient, kernel_info):
        """Check spyder-kernels version."""
        self.__requested_info = False
        if kernel_info:
            try:
                kernel_handler = KernelHandler.from_connection_info(
                    kernel_info["connection_info"],
                    ssh_connection=self._plugin.get_remote_server(
                        ipyclient.server_id
                    )._ssh_connection,
                )
                kernel_handler.set_time_to_dead(1.0)
            except Exception as err:
                ipyclient.remote_kernel_restarted_failure_message(
                    err, shutdown=True
                )
            else:
                ipyclient.replace_kernel(
                    kernel_handler, shutdown_kernel=False, clear=False
                )
        else:
            ipyclient.remote_kernel_restarted_failure_message(shutdown=True)

            # This will show an error message in the plugins connected to the
            # IPython console and disable kernel related actions in its Options
            # menu.
            sw = ipyclient.shellwidget
            sw.sig_shellwidget_errored.emit(sw)

    def _on_client_message_logged(self, message: dict):
        """Actions to take when a client message is logged."""
        msg_id = message["id"]

        # Create deque if not available
        if not self.client_logs.get(msg_id):
            self.client_logs[msg_id] = deque([], MAX_CLIENT_MESSAGES)

        # Add message to deque
        self.client_logs[msg_id].append(message)
