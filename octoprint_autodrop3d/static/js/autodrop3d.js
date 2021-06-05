/*
 * View model for Autodrop3D
 *
 * Author: Autodrop3D
 * License: MIT
 */
$(function() {
    function autodrop3dViewModel(parameters) {
        const self = this;

        self.settingsViewModel = parameters[0];
        self.polling_enabled = ko.observable(false);
        self.processing = ko.observable(false);
        self.getting_url = ko.observable(false);
        self.job_file = ko.observable(false);
        self.job_status = ko.observable(false);
        self.new_at_command = ko.observable('');
        self.selected_command = ko.observable();
        self.print_events = {PrintStarted: 'started', PrintCancelled: 'has been canceled', PrintFailed: 'has failed', PrintDone: 'complete', Error: 'errored'}
        self.job_id = ko.pureComputed(function(){
            return self.job_file().replace(/^.*\/(.*).gcode$/gm, '$1');
        })
        self.buttonTitle = ko.pureComputed(function(){
            return (self.processing()) ? "Processing" : (self.polling_enabled()) ? "Disconnect Autodrop3D" : "Connect Autodrop3D";
        })
        self.buttonPointer = ko.pureComputed(function(){
            return (self.processing()) ? "wait" : "pointer";
        })
        self.get_autodrop_url = ko.pureComputed(function(){
            return self.settingsViewModel.settings.plugins.autodrop3d.server().replace(/^(.*)\/api\/jobsQueue\/printerRequestJob$/gm, '$1')
        })
        // Hack to remove automatically added Cancel button
        // See https://github.com/sciactive/pnotify/issues/141
        PNotify.prototype.options.confirm.buttons = [];
        // configure default PNotify options
        self.pnotify_options = {
            title: gettext('Autodrop3D'),
            type: 'info',
            icon: true,
            hide: false,
            confirm: {
                confirm: true,
                buttons: [{
                    text: gettext('Yes'),
                    addClass: 'btn-block',
                    promptTrigger: true,
                    click: function (notice, value) {
                        notice.remove();
                        notice.get().trigger("pnotify.cancel", [notice, value]);
                    }
                }]
            },
            buttons: {
                closer: false,
                sticker: false,
            },
            history: {
                history: false
            }
        };

        self.onBeforeBinding = function(){
            self.polling_enabled(self.settingsViewModel.settings.plugins.autodrop3d.polling_enabled());
        }

        self.onDataUpdaterPluginMessage = function(plugin, data) {
            if (plugin != "autodrop3d") {
                return;
            }

            if(data.status){
                self.job_file(data.filename);
                self.job_status(data.status);
                // inject options into the default PNotify options.
                switch(data.status){
                    case "PrintStarted":
                        self.pnotify_options.type = 'info';
                        self.pnotify_options.hide = true;
                        self.pnotify_options.text = '<div class="row-fluid"><p>Job "' + self.job_id() + '" has ' + self.print_events[data.status] + '.</p></div>';
                        self.pnotify_options.confirm.buttons[0].text = 'Ok';
                        self.pnotify_options.confirm.buttons[0].addClass = 'btn-block btn-primary';
                        break;
                    case "PrintCancelled":
                    case "PrintFailed":
                        self.pnotify_options.type = 'error';
                        self.pnotify_options.hide = false;
                        self.pnotify_options.text = '<div class="row-fluid"><p>Job "' + self.job_id() + '" ' + self.print_events[data.status] + '.</p><p>Has the bed been cleared to continue?</p></div>';
                        self.pnotify_options.confirm.buttons[0].text = 'Yes';
                        self.pnotify_options.confirm.buttons[0].addClass = 'btn-block btn-danger';
                        self.processing(true);
                        self.polling_enabled(false);
                        break;
                    case "PrintDone":
                        self.pnotify_options.type = 'success';
                        self.pnotify_options.hide = false;
                        self.pnotify_options.text = '<div class="row-fluid"><p>Job "' + self.job_id() + '" is ' + self.print_events[data.status] + '.</p><p>Has the bed been cleared to continue?</p></div>';
                        self.pnotify_options.confirm.buttons[0].text = 'Yes';
                        self.pnotify_options.confirm.buttons[0].addClass = 'btn-block btn-success';
                        break;
                    case "Error":
                        self.pnotify_options.type = 'error';
                        self.pnotify_options.hide = false;
                        self.pnotify_options.text = '<div class="row-fluid"><p>An error has occurred, please check octoprint.log.</p></div>';
                        self.pnotify_options.confirm.buttons[0].text = 'Ok';
                        self.pnotify_options.confirm.buttons[0].addClass = 'btn-block btn-danger';
                        break;
                    default:
                        // unknown status exit gracefully
                        self._logger("info", data)
                        return;
                }

                self.pnotify_popup = new PNotify(self.pnotify_options);
                if(["PrintCancelled", "PrintFailed", "PrintDone"].indexOf(data.status) > -1){
                    self.pnotify_popup.get().on('pnotify.cancel', function() {
                        self.bed_cleared();
                    });
                } else {
                    self.pnotify_popup.get().on('pnotify.cancel', function() {
                        self._logger("info", [self.job_file(), data.status]);
                    });
                }
            }
        }

        self.bed_cleared = function(){
            // send message that bed is clear
            OctoPrint.simpleApiCommand("autodrop3d", "bed_cleared", {filename: self.job_file()})
                .done(function (response) {
                    self._logger("info", response);
                    if(response.bed_cleared){
                        self.job_file(false);
                        self.processing(false);
                        self.polling_enabled(response.enabled);
                    }
                });
        }

        self.toggle_polling = function(){
            self.processing(true);
            let command = self.polling_enabled() ? "disconnect" : "connect";
            let payload = {job_status: (self.job_status() && self.job_status() !== '') ? self.job_status() : ''};
            self._logger("info", [command, payload]);
            OctoPrint.simpleApiCommand("autodrop3d", command, payload)
                .done(function (response) {
                    self._logger("info", response.enabled);
                    self.polling_enabled(response.enabled);
                }).always(function(response){
                    self.processing(false);
                });
        }

        self.get_default_server_url = function(data){
            self.getting_url(true);
            OctoPrint.simpleApiCommand("autodrop3d", "get_default_server_url")
                .done(function (response) {
                    self._logger("info", response);
                    if(response.url){
                        self.settingsViewModel.settings.plugins.autodrop3d.server(response.url);
                    }
                })
                .always(function(){
                    self.getting_url(false);
                });
        }

        self._logger = function(type, message){
            switch (type){
                case "log":
                case "info":
                    console.log("autodrop3d", message);
                    break
                case "debug":
                    console.debug("autodrop3d", message);
                    break
                default:
                    console.error("autodrop3d", "unsupported logging type, message was: " + message);
            }
        }

        self.add_at_command = function(){
            self.selected_command({'command': ko.observable(self.new_at_command()), 'python': ko.observable('')});
            self.settingsViewModel.settings.plugins.autodrop3d.at_commands_to_monitor.push(self.selected_command());
            $('#autodrop3d_command_editor').modal('show');
            self.new_at_command('');
        }

        self.edit_at_command = function(data){
            self.selected_command(data);
            $('#autodrop3d_command_editor').modal('show');
            console.log(self.selected_command());
        }

        self.remove_at_command = function(data){
            self.settingsViewModel.settings.plugins.autodrop3d.at_commands_to_monitor.remove(data);
        }
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: autodrop3dViewModel,
        dependencies: [ "settingsViewModel" ],
        elements: [ "#settings_plugin_autodrop3d", "#navbar_plugin_autodrop3d", "#autodrop3d_command_editor" ]
    });
});
