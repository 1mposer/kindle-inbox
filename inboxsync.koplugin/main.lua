--[[--
Book inbox: pulls books sent from a phone (via the kindle-inbox GitHub repo)
into <home folder>/Inbox.

It never turns Wi-Fi on by itself and never prompts. It checks:
  * shortly after KOReader starts, if Wi-Fi is already connected
  * when Wi-Fi connects (including KOReader's auto-restore after waking up)
  * a few times in the first minute after waking up
  * every 30 minutes while the device is awake and connected
  * on demand: Tools > Book inbox > Check now

The feed URL defaults to DEFAULT_FEED below; a one-line feed_url.txt next to this
file overrides it.

@module koplugin.inboxsync
--]]

local DataStorage = require("datastorage")
local Dispatcher = require("dispatcher")
local InfoMessage = require("ui/widget/infomessage")
local LuaSettings = require("luasettings")
local NetworkMgr = require("ui/network/manager")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local logger = require("logger")
local util = require("util")
local _ = require("gettext")
local T = require("ffi/util").template

local DEFAULT_FEED = "https://1mposer.github.io/kindle-inbox/manifest.json"
local PERIOD_MIN = 30
local RESUME_DELAYS = { 15, 35, 70 }

-- Shared by every instance (one lives in the file manager, one in the reader).
local state = { running = false, last_ok = 0 }

local InboxSync = WidgetContainer:extend{
    name = "inboxsync",
    is_doc_only = false,
}

local function now()
    return os.time()
end

function InboxSync:init()
    self.settings = LuaSettings:open(DataStorage:getSettingsDir() .. "/inboxsync.lua")
    self.feed_url = self:readFeedUrl()
    self.periodic = function() self:onPeriodic() end
    self.auto_check = function() self:maybeSync(120) end
    self:onDispatcherRegisterActions()
    if self.ui and self.ui.menu then
        self.ui.menu:registerToMainMenu(self)
    end
    if self:isAuto() then
        -- Startup / switching between library and book: do not hammer the server.
        UIManager:scheduleIn(8, function() self:maybeSync(600) end)
        UIManager:scheduleIn(PERIOD_MIN * 60, self.periodic)
    end
end

function InboxSync:readFeedUrl()
    local path = (self.path or "plugins/inboxsync.koplugin") .. "/feed_url.txt"
    local f = io.open(path, "r")
    if f then
        local line = f:read("*l")
        f:close()
        if line then
            line = line:gsub("^%s+", ""):gsub("%s+$", "")
            if line:match("^https?://") then
                return line
            end
        end
    end
    return DEFAULT_FEED
end

function InboxSync:isAuto()
    return self.settings:nilOrTrue("auto")
end

function InboxSync:inboxDir()
    local home = G_reader_settings:readSetting("home_dir") or "/mnt/us/documents"
    return home:gsub("/$", "") .. "/Inbox"
end

function InboxSync:onDispatcherRegisterActions()
    Dispatcher:registerAction("inboxsync_check", {
        category = "none", event = "InboxSyncCheck", title = _("Check book inbox"), general = true,
    })
end

function InboxSync:addToMainMenu(menu_items)
    menu_items.inboxsync = {
        text = _("Book inbox"),
        sorting_hint = "tools",
        sub_item_table = {
            {
                text = _("Check now"),
                callback = function() self:onInboxSyncCheck() end,
            },
            {
                text = _("Check automatically"),
                checked_func = function() return self:isAuto() end,
                callback = function()
                    self.settings:saveSetting("auto", not self:isAuto())
                    self.settings:flush()
                end,
            },
            {
                text = _("About"),
                keep_menu_open = true,
                callback = function()
                    local last = self.settings:readSetting("last_check")
                    UIManager:show(InfoMessage:new{
                        text = T(_("Books sent from your phone arrive in:\n%1\n\nFeed:\n%2\n\nLast check: %3\n\nChecks happen on wake and Wi-Fi connect, and every %4 minutes while awake. Nothing downloads while the Kindle is asleep."),
                            self:inboxDir(), self.feed_url,
                            last and os.date("%Y-%m-%d %H:%M", last) or _("never"), PERIOD_MIN),
                    })
                end,
            },
        },
    }
end

-- Manual check: allowed to turn Wi-Fi on the normal KOReader way.
function InboxSync:onInboxSyncCheck()
    NetworkMgr:runWhenOnline(function()
        self:sync(true)
    end)
    return true
end

function InboxSync:maybeSync(min_gap)
    if not self:isAuto() or state.running then return end
    if now() - state.last_ok < (min_gap or 60) then return end
    if not NetworkMgr:isConnected() then return end
    if not NetworkMgr:isOnline() then return end
    self:sync(false)
end

function InboxSync:onNetworkConnected()
    UIManager:scheduleIn(3, self.auto_check)
end

function InboxSync:onResume()
    if not self:isAuto() then return end
    for __, d in ipairs(RESUME_DELAYS) do
        UIManager:scheduleIn(d, function() self:maybeSync(60) end)
    end
    UIManager:unschedule(self.periodic)
    UIManager:scheduleIn(PERIOD_MIN * 60, self.periodic)
end

function InboxSync:onSuspend()
    UIManager:unschedule(self.periodic)
end

function InboxSync:onPeriodic()
    self:maybeSync(60)
    UIManager:scheduleIn(PERIOD_MIN * 60, self.periodic)
end

function InboxSync:onCloseWidget()
    UIManager:unschedule(self.periodic)
    UIManager:unschedule(self.auto_check)
end

-- HTTP GET into a sink; returns status code (number) or nil, err.
local function httpGet(url, sink, big)
    local http = require("socket.http")
    local ltn12 = require("ltn12")
    local socket = require("socket")
    local socketutil = require("socketutil")
    if big then
        socketutil:set_timeout(socketutil.FILE_BLOCK_TIMEOUT, socketutil.FILE_TOTAL_TIMEOUT)
    else
        socketutil:set_timeout(socketutil.LARGE_BLOCK_TIMEOUT, socketutil.LARGE_TOTAL_TIMEOUT)
    end
    local ok, code = pcall(function()
        return socket.skip(1, http.request{
            url = url,
            method = "GET",
            headers = { ["Accept-Encoding"] = "identity", ["User-Agent"] = socketutil.USER_AGENT },
            sink = sink or ltn12.sink.null(),
        })
    end)
    socketutil:reset_timeout()
    if not ok then return nil, tostring(code) end
    return code
end

local function safeName(s)
    s = tostring(s or ""):match("([^/]+)$") or ""
    s = s:gsub("[^%w%-_%.]", "_")
    if s == "" or s:sub(1, 1) == "." then return nil end
    if not s:lower():match("%.epub$") then s = s .. ".epub" end
    return s
end

function InboxSync:sync(interactive)
    if state.running then return end
    state.running = true
    local ok, err = pcall(function() self:_sync(interactive) end)
    state.running = false
    if not ok then
        logger.warn("InboxSync: error", err)
        if interactive then
            UIManager:show(InfoMessage:new{ text = T(_("Book inbox: %1"), tostring(err)) })
        end
    end
end

function InboxSync:_sync(interactive)
    local ltn12 = require("ltn12")
    local json = require("json")

    local chunks = {}
    local sep = self.feed_url:find("?", 1, true) and "&" or "?"
    local code, err = httpGet(self.feed_url .. sep .. "t=" .. now(), ltn12.sink.table(chunks), false)
    if code ~= 200 then
        error(T(_("could not read the feed (%1)"), tostring(code or err)))
    end
    local okj, manifest = pcall(json.decode, table.concat(chunks))
    if not okj or type(manifest) ~= "table" or type(manifest.books) ~= "table" then
        error(_("the feed is not valid"))
    end

    local base = self.feed_url:gsub("[?#].*$", ""):gsub("/[^/]*$", "")
    local seen = self.settings:readSetting("seen") or {}
    local dir = self:inboxDir()
    local pending = {}
    for __, b in ipairs(manifest.books) do
        if type(b) == "table" and type(b.id) == "string" and not seen[b.id] then
            local name = safeName(b.file)
            if name and type(b.file) == "string" and not b.file:find("%.%.") then
                table.insert(pending, { book = b, name = name })
            end
        end
    end

    local added = {}
    if #pending > 0 then
        util.makePath(dir)
        local msg = InfoMessage:new{ text = T(_("Downloading %1 book(s) to Inbox…"), #pending), timeout = 3 }
        UIManager:show(msg)
        UIManager:forceRePaint()
        for __, p in ipairs(pending) do
            local target = dir .. "/" .. p.name
            if util.fileExists(target) then
                seen[p.book.id] = true
            else
                local part = target .. ".part"
                local fh = io.open(part, "wb")
                if fh then
                    local c = httpGet(base .. "/" .. p.book.file, ltn12.sink.file(fh), true)
                    local good = false
                    if c == 200 then
                        local rf = io.open(part, "rb")
                        if rf then
                            good = rf:read(2) == "PK"
                            rf:close()
                        end
                    end
                    if good and os.rename(part, target) then
                        seen[p.book.id] = true
                        table.insert(added, p.book.title or p.name)
                    else
                        os.remove(part)
                        logger.warn("InboxSync: download failed", p.book.file, c)
                    end
                end
            end
        end
        UIManager:close(msg)
    end

    self.settings:saveSetting("seen", seen)
    self.settings:saveSetting("last_check", now())
    self.settings:flush()
    state.last_ok = now()

    if #added > 0 then
        local text
        if #added == 1 then
            text = T(_("New in Inbox: %1"), added[1])
        else
            text = T(_("%1 new books in Inbox"), #added)
        end
        UIManager:show(InfoMessage:new{ text = text, timeout = 4 })
        if self.ui and self.ui.file_chooser then
            self.ui.file_chooser:refreshPath()
        end
    elseif interactive then
        UIManager:show(InfoMessage:new{ text = _("Book inbox is up to date."), timeout = 2 })
    end
end

return InboxSync
