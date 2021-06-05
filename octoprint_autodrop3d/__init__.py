# coding=utf-8
from __future__ import absolute_import

import base64
import io
import re
import flask
import octoprint.plugin
import requests
from flask_babel import gettext
from octoprint.access.permissions import Permissions, ADMIN_GROUP
from octoprint.events import Events
from octoprint.util import RepeatedTimer


class autodrop3d(
	octoprint.plugin.SettingsPlugin,
	octoprint.plugin.AssetPlugin,
	octoprint.plugin.TemplatePlugin,
	octoprint.plugin.SimpleApiPlugin,
	octoprint.plugin.EventHandlerPlugin,
	octoprint.plugin.ProgressPlugin
):

	# ~~ Initialize

	def __init__(self):
		self.autodrop3d_enabled = False
		self.printer_id = None
		self.printer_api_key = None
		self.auto_eject_active = False
		self.server_url = None
		self.at_commands_to_monitor = None
		self.at_commands_to_process = {}
		self.polling_interval = 0
		self.job_queue_timer = None
		self.job_queue_polling = False
		self.print_status_timer = None
		self.print_status_polling = False
		self.ip = None
		self.job_id = None
		self.job_status = None
		self.bed_clear = True
		self.current_job = None
		self.active_thread = None
		self._snapshot_url = None
		self._snapshot_timeout = None
		self._snapshot_validate_ssl = None
		self._snapshot_filename = None
		self.regex_jobid_extract = re.compile(r"^.*/(.*).gcode$") #use this to extract the job_id from filename
		self.autodrop3d_return_codes = {"CANCELED": "CANCELED", "RECORDED": "RECORDED"}

	def initialize_settings(self):
			self.job_queue_polling = self._settings.get_boolean(["polling_enabled"])
			self.autodrop3d_enabled = self.job_queue_polling
			self.auto_eject_active = self._settings.get_boolean(["auto_eject_active"])
			self._snapshot_url = self._settings.global_get(["webcam", "snapshot"])
			self._snapshot_timeout = self._settings.global_get_int(["webcam", "snapshotTimeout"])
			self._snapshot_validate_ssl = self._settings.global_get_boolean(["webcam", "snapshotSslValidation"])
			self._snapshot_filename = "{}/snapshot.png".format(self._settings.get_plugin_data_folder())
			self.printer_id = self._settings.get(["name"])
			self.printer_api_key = self._settings.get(["key"])
			self.polling_interval = self._settings.get_float(["polling_interval"])
			self.server_url = self._settings.get(["server"])
			self.at_commands_to_monitor = self._settings.get(["at_commands_to_monitor"])

	# ~~ EventHandlerPlugin mixin

	def on_event(self, event, payload):
		if event not in [Events.CONNECTED, Events.DISCONNECTED, Events.PRINT_STARTED, Events.PRINT_CANCELLED,
						 Events.PRINT_DONE, Events.CONNECTIVITY_CHANGED, Events.STARTUP, Events.SETTINGS_UPDATED]:
			return
		self.job_status = event
		if event == Events.STARTUP:
			# Server Startup Initialize Variables
			self.initialize_settings()
		if event == Events.CONNECTED:
			# Printer Connected
			if self.autodrop3d_enabled and self.job_queue_timer is None:
				self._logger.debug("printer connected, starting repeated timer")
				self.job_queue_polling, self.job_queue_timer = self.start_repeated_timer(self.job_queue_timer, self.job_queue_worker)
			if self.current_job and self.autodrop3d_enabled and self.job_queue_polling:
				# handle race condition where if octoprint was up and printer was disconnected
				self._logger.debug("printer connected, starting queued job \"{}\"".format(self.current_job[0]))
				self._printer.select_file(self.current_job, False, printAfterSelect=True)
		if event == Events.DISCONNECTED and self.job_queue_timer is not None:
			# Printer Disconnected
			self._logger.debug("printer disconnected")
			self.job_queue_polling, self.job_queue_timer = self.stop_repeated_timer(self.job_queue_timer)
		if event == Events.CONNECTIVITY_CHANGED:
			# Internet Connectivity Changed
			if payload["new"] and self._settings.get_boolean(["polling_enabled"]) and self._printer.is_ready():
				self._logger.debug("internet up start polling")
				self.autodrop3d_enabled = True
				self.job_queue_polling, self.job_queue_timer = self.start_repeated_timer(self.job_queue_timer, self.job_queue_worker)
			else:
				self._logger.debug("no internet available stop polling")
				self.job_queue_polling, self.job_queue_timer = self.stop_repeated_timer(self.job_queue_timer)
				self.print_status_polling, self.print_status_timer = self.stop_repeated_timer(self.print_status_timer)
				self.autodrop3d_enabled = False
			# possibly send_plugin_message to front end?
		if event == Events.PRINT_STARTED and self.current_job:
			if payload["path"].startswith("Autodrop3D"):
				# prevent downloading a bunch of jobs from online queue
				self._logger.debug("print started, no need to poll job queue")
				self.job_queue_polling, self.job_queue_timer = self.stop_repeated_timer(self.job_queue_timer)
				self.print_status_polling, self.print_status_timer = self.start_repeated_timer(self.print_status_timer, self.print_status_worker)
				self._plugin_manager.send_plugin_message(self._identifier, dict(filename=payload["path"], status=event))
		if event == Events.PRINT_DONE and self.current_job:
			# print completed, start job queue timer
			self._logger.debug("print job event {} for \"{}\"".format(event, payload["path"]))
			self.print_status_polling, self.print_status_timer = self.stop_repeated_timer(self.print_status_timer)
			if self.autodrop3d_enabled:
				self.job_queue_polling, self.job_queue_timer = self.start_repeated_timer(self.job_queue_timer, self.job_queue_worker)
			if self._settings.get_boolean(["notify_complete"]):
				self._logger.debug("writing notify_complete.txt")
				with open(self.get_plugin_data_folder() + "/" + "notifyComplete.txt", "w") as f:
					f.write("")
			if len(self._settings.get(["custom_script"])) > 0:
				self._logger.debug("{}".format(self._settings.get(["custom_script"])))
				exec("{}".format(self._settings.get(["custom_script"])))
			if not self.auto_eject_active:
				self._plugin_manager.send_plugin_message(self._identifier, dict(filename=payload["path"], status=event))
		if event == Events.PRINT_CANCELLED and self.current_job:
			self._logger.debug("print job event {} for \"{}\"".format(event, payload["path"]))
		if event == Events.SETTINGS_UPDATED:
			self.initialize_settings()
			self._logger.debug("settings updated: {}".format(payload))

	# ~~ Autodrop3D workers

	def print_status_worker(self):
			self._logger.debug("polling print status queue")
			# get current print state data ot send back to cloud
			_current_data = self._printer.get_current_data()
			_status = _current_data["progress"]["completion"]
			if self.job_status in [Events.PRINT_FAILED, Events.PRINT_CANCELLED]:
				_status = "canceled"
			self._logger.debug("get_current_data: {}".format(_current_data))
			data = {
				"jobID": self.regex_jobid_extract.sub("\\1", _current_data["job"]["file"]["path"]),  # extract job id from filename
				"name": self.printer_id,
				"key": self.printer_api_key,
				"stat": "update",
				"jobStatus": _status,
				"img": self._image_to_data_url(),
				"ip": self._get_ip(),
			}
			headers = {"Content-type": "application/json", "Accept": "text/plain"}
			response = requests.post(self.server_url, json=data, headers=headers)
			self._logger.debug("autodrop3d response: {}".format(response.text))
			if response.status_code == 200:
				if response.text == self.autodrop3d_return_codes["CANCELED"]:
					if self._printer.is_printing():
						self._printer.cancel_print()
					self.print_status_polling, self.print_status_timer = self.stop_repeated_timer(self.print_status_timer)
					if self.autodrop3d_enabled:
						self.job_queue_polling, self.job_queue_timer = self.start_repeated_timer(self.job_queue_timer, self.job_queue_worker)
					if _status == "canceled":
						_status = 0
					self._logger.debug("print status {} at {:0.2f}%".format(response.text, _status))
					if not self.auto_eject_active:
						self._plugin_manager.send_plugin_message(self._identifier, dict(filename=_current_data["job"]["file"]["path"], status=Events.PRINT_CANCELLED))
			else:
				self._logger.debug("error communicating: {}".format(response.text))
				# notify the UI in case of download error
				self._plugin_manager.send_plugin_message(self._identifier, dict(error=response, status="ERROR"))

	#def delete_file(self, filename):

	def job_queue_worker(self):
		if not self.bed_clear:
			if not self.auto_eject_active:
				self._logger.debug("bypass polling since bed is not clear")
				return
			else:
				try: # hack for windows where file can't be remove because it's in use
					self._file_manager.remove_file("local", self.current_job + ".gcode" )
				except Exception as e:
					self._logger.error("unabled to delete job file \"{}\" from local storage".format(self.current_job + ".gcode" ))
					pass
				self.bed_clear = True
				if self.job_status in [Events.PRINT_FAILED, Events.PRINT_CANCELLED]:
					self.job_status = None
				self.job_status = None
				download_url = "{}?name={}&key={}&ip={}&jobID={}&stat=Done".format(
					self.server_url,
					self.printer_id,
					self.printer_api_key,
					self._get_ip(),
					self.regex_jobid_extract.sub("\\1", self.current_job)
				)
				response = requests.get(download_url)
				self.current_job = None
				if response.status_code == 200:
					self._logger.debug("server responded: {}".format(response.text))
					#return flask.jsonify({"bed_cleared": True, "enabled": self.autodrop3d_enabled})
				else:
					self._logger.debug("server responded: {}".format(response.text))
					#return flask.jsonify({"unknown response": data["filename"]})
				return



		self._logger.debug("polling job queue")
		download_url = "{}?name={}&key={}&ip={}".format(
			self.server_url,
			self.printer_id,
			self.printer_api_key,
			self._get_ip(),
		)
		response = requests.get(download_url)
		if response.status_code == 200:
			# Save file to plugin's data folder for processing
			download_file_name = self.get_plugin_data_folder() + "/" + "download.gcode"
			self._logger.debug("saving file: %s" % download_file_name)
			with open(download_file_name, "w") as f:
				f.write(response.text)
			f = open(download_file_name, "r")
			server_message = str(f.readline())
			if server_message.find(";START") == -1:
				self._logger.debug("no job queued")
				f.close()
			else:
				f.readline()  # strip blank line after ;START
				self.job_id = (str(f.readline()).replace(";", "").strip())

				f.close()
				self._logger.debug("received jobID: {}".format(self.job_id))

				file_wrapper = octoprint.filemanager.util.DiskFileWrapper("download.gcode", download_file_name)
				job_file = self._file_manager.add_file("local", "Autodrop3D/{}.gcode".format(self.job_id), file_wrapper, allow_overwrite=True)
				self.current_job = job_file
				self._logger.debug("added job file \"{}\" to local storage".format(job_file))
				if self._printer.is_ready() and self.bed_clear:
					self.bed_clear = False
					self._logger.debug("starting print job \"{}\"".format(job_file))
					self._printer.select_file(job_file, False, printAfterSelect=True)
				else:
					self._logger.debug("unable to start print job \"{}\" because printer isn't ready, queuing".format(job_file))
		elif response.status_code == 404:
			self._logger.debug("no job queued")
			return
		else:
			self._logger.debug("error downloading, status_code: {}, text: ".format(response.status_code, response.text))
			# notify the UI in case of download error
			self._plugin_manager.send_plugin_message(self._identifier, dict(error=response, status="ERROR"))

	# ~~ Utility Functions

	def _image_to_data_url(self):
		if self._snapshot_url is not None and self._snapshot_url.strip() != "":
			try:
				self._logger.debug(
					"Going to capture {} from {}".format(self._snapshot_filename, self._snapshot_url)
				)
				r = requests.get(
					self._snapshot_url,
					stream=True,
					timeout=self._snapshot_timeout,
					verify=self._snapshot_validate_ssl,
				)
				r.raise_for_status()

				with io.open(self._snapshot_filename, "wb") as f:
					for chunk in r.iter_content(chunk_size=1024):
						if chunk:
							f.write(chunk)
							f.flush()

				self._logger.debug("Image {} captured from {}".format(self._snapshot_filename, self._snapshot_url))
			except Exception as e:
				self._logger.exception("Could not capture image {} from {}".format(self._snapshot_filename, self._snapshot_url))
				img_file = "{}/static/img/no_camera.png".format(self._basefolder)
				err = e
			else:
				img_file = self._snapshot_filename
				err = None
		else:
			img_file = "{}/static/img/no_camera.png".format(self._basefolder)
		return "data:image/png;base64," + base64.b64encode(open(img_file, "rb").read()).decode("utf8")

	def _get_ip(self):
		if self.ip is not None:
			# ip address already retrieved
			return self.ip
		import socket
		server_ip = [(s.connect((self._settings.global_get(["server", "onlineCheck", "host"]),
								 self._settings.global_get(["server", "onlineCheck", "port"]),)),
					  s.getsockname()[0],
					  s.close(),) for s in [socket.socket(socket.AF_INET, socket.SOCK_DGRAM)]][0][1]
		self.ip = server_ip
		return server_ip

	def _continue_polling(self):
		self._logger.debug("continue polling: {}".format(self.autodrop3d_enabled))
		return self.autodrop3d_enabled

	def _polling_canceled(self):
		self._logger.debug("polling canceled, autodrop3d enabled? {}".format(self.autodrop3d_enabled))

	def start_repeated_timer(self, timer=None, callback=None):
		try:
			if timer is None and callback is not None:
				self._logger.debug("creating repeated timer")
				timer = RepeatedTimer(self.polling_interval, callback, run_first=True, condition=self._continue_polling, on_condition_false=self._polling_canceled)
				timer.start()
			return True, timer
		except Exception:
			return False, timer

	def stop_repeated_timer(self, timer=None):
		try:
			if timer is not None:
				self._logger.debug("stopping repeated timer")
				timer.cancel()
				timer = None
			return False, timer
		except Exception:
			return True, timer

	# ~~ gcode queing hook

	def gcode_queueing_handler(self, comm_instance, phase, cmd, cmd_type, gcode, *args, ** kwargs):
		if not any(map(lambda r: r["command"] == cmd.split()[0].replace("@", ""), self.at_commands_to_monitor)):
			return

		return ["M400", "M118 AUTODROP3D {}".format(cmd.split()[0].replace("@", "")), "@pause"]

	# ~~ gcode received hook

	def gcode_received_handler(self, comm, line, *args, **kwargs):
		if not line.startswith("AUTODROP3D"):
			return line

		command_list = line.split()[1:]
		command = command_list[0]
		parameters = command_list[1:]
		if command:
			for at_command in self.at_commands_to_monitor:
				if at_command["command"] == command:
					self._logger.debug("received @ command: \"{}\" with parameters: \"{}\"".format(command, parameters))
					try:
						exec("{}".format(at_command["python"]))
					except Exception as e:
						self._logger.debug(e)
					if self._printer.is_paused():
						self._printer.resume_print()
		return line

	# ~~ SimpleApiPlugin mixin

	def get_api_commands(self):
		return dict(disconnect=[], connect=[], get_default_server_url=[], bed_cleared=["filename"])

	def on_api_command(self, command, data):
		if not Permissions.PLUGIN_AUTODROP3D_CONTROL.can():
			return flask.make_response("Insufficient rights", 403)

		if command == "disconnect":
			self._logger.debug("stop polling per user request")
			self.autodrop3d_enabled = False
			if self.job_queue_timer is not None:
				self.job_queue_polling, self.job_queue_timer = self.stop_repeated_timer(self.job_queue_timer)
		if command == "connect":
			self._logger.debug("start polling per user request")
			self.autodrop3d_enabled = True
			self.job_queue_polling, self.job_queue_timer = self.start_repeated_timer(self.job_queue_timer, self.job_queue_worker)
		if command == "get_default_server_url":
			return flask.jsonify({"url": self.get_settings_defaults()["server"]})
		if command == "bed_cleared" and data["filename"]:
			if data["filename"]:
				try: # hack for windows where file can't be remove because it's in use
					self._file_manager.remove_file("local", data["filename"])
					self._logger.debug("deleted job file \"{}\" from local storage".format(data["filename"]))
				except Exception as e:
					self._logger.error("unabled to delete job file \"{}\" from local storage".format(data["filename"]))
					pass
				self.bed_clear = True
				if self.job_status in [Events.PRINT_FAILED, Events.PRINT_CANCELLED]:
					self.job_status = None
					return flask.jsonify({"bed_cleared": True, "enabled": self.autodrop3d_enabled})
				self.job_status = None
				download_url = "{}?name={}&key={}&ip={}&jobID={}&stat=Done".format(
					self.server_url,
					self.printer_id,
					self.printer_api_key,
					self._get_ip(),
					self.regex_jobid_extract.sub("\\1", data["filename"])
				)
				response = requests.get(download_url)
				self.current_job = None
				if response.status_code == 200:
					self._logger.debug("server responded: {}".format(response.text))
					return flask.jsonify({"bed_cleared": True, "enabled": self.autodrop3d_enabled})
				else:
					self._logger.debug("server responded: {}".format(response.text))
					return flask.jsonify({"unknown response": data["filename"]})

		if self._settings.get_boolean(["polling_enabled"]) != self.autodrop3d_enabled:
			self._settings.set_boolean(["polling_enabled"], self.autodrop3d_enabled)
			self._settings.save(trigger_event=True)
		response = {"enabled": self.autodrop3d_enabled}
		return flask.jsonify(response)

	# ~~ Access Permissions Hook

	def get_additional_permissions(self, *args, **kwargs):
		return [
			dict(
				key="CONTROL",
				name="Control Connection",
				description=gettext("Allows control of the Autodrop3D connection."),
				roles=["admin"],
				dangerous=True,
				default_groups=[ADMIN_GROUP],
			)
		]

	# ~~ SettingsPlugin mixin

	def get_settings_defaults(self):
		return dict(
			name="", key="", server="https://go.autodrop3d.com/api/jobsQueue/printerRequestJob", polling_enabled=True,
			polling_interval=10, custom_script="", notify_complete=False, use_gpio=False, at_commands_to_monitor=[]
		)

	# ~~ AssetPlugin mixin

	def get_assets(self):
		return dict(js=["js/autodrop3d.js"], css=["css/autodrop3d.css"])

	# ~~ TemplatePlugin mixin

	def get_template_configs(self):
		return [
			dict(type="navbar", custom_bindings=True),
			dict(type="settings", custom_bindings=True),
		]

	# ~~ Softwareupdate hook

	def get_update_information(self):
		return dict(
			autodrop3d=dict(
				displayName="Autodrop3D",
				displayVersion=self._plugin_version,
				# version check: github repository
				type="github_release",
				user="Autodrop3d",
				repo="AD3d-octoprint-connect",
				current=self._plugin_version,
				stable_branch=dict(
					name="Stable", branch="master", comittish=["master"]
				),
				prerelease_branches=[
					dict(
						name="Release Candidate",
						branch="rc",
						comittish=["rc", "master"],
					)
				],
				# update method: pip
				pip="https://github.com/Autodrop3d/AD3d-octoprint-connect/archive/{target_version}.zip",
			)
		)


__plugin_name__ = "Autodrop3D"
__plugin_pythoncompat__ = ">=3,<4"


def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = autodrop3d()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.access.permissions": __plugin_implementation__.get_additional_permissions,
		"octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.gcode_queueing_handler,
		"octoprint.comm.protocol.gcode.received": __plugin_implementation__.gcode_received_handler,
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
	}
