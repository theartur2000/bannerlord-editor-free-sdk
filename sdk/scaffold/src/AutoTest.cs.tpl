// Unattended test driver for the SDK's harness (harness/night_test.ps1, or `bl run`).
//
// When the environment variable BANNERLORD_AUTOTEST is 1 and Modules/__MOD__/autostart.txt exists, the words in that
// file are run and the game quits by itself:
//   menu=<s>     wait that many seconds at the main menu first (time for an outside tool, for example to fill a scene slot)
//   type=<id>    the Custom Battle game type before the map: Battle, Siege, Village (the ids the screen offers)
//   scene=<id>   the Custom Battle map to fight on (a scene listed in ModuleData/__MODLOWER___custom_battle_scenes.xml)
//   tod=<hour>   time of day of the battle (the nearest one Custom Battle offers)
//   season=<id>  season of the battle: summer, fall, winter, spring
//   views        before anything else in the battle, look at the scene from the viewpoints listed in the scene folder's
//                views.txt (name, eye x y z, target x y z per line) and take a screenshot of each
//   fight        finish deployment, order the player's side to charge, let the AI play the player
//   secs=<n>     seconds of battle before quitting (default 60)
//   quit         quit the game at the end (without it the battle stays open)
// Screenshots go to <SDK home>\out\__MODLOWER___shots; every 10 s one log line counts the troops alive on both sides.
// The last line is AUTOTEST_DONE, the marker the harness waits for.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using TaleWorlds.Core;
using TaleWorlds.Engine;
using TaleWorlds.Library;
using TaleWorlds.MountAndBlade;
using TaleWorlds.MountAndBlade.CustomBattle;
using TaleWorlds.MountAndBlade.View.Screens;
using TaleWorlds.ScreenSystem;
using Path = System.IO.Path;

namespace __MOD__
{
    static class AutoTest
    {
        public static bool On;
        public static string Words = "";
        public static Action Deferred;
        static float _stageT;
        static int _stage;
        static string _lastScreen;

        static string[] All => Words.Split(new[] { ' ', '\t', '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries);

        public static string Word(string key)
        {
            foreach (string w in All)
                if (w.StartsWith(key + "=", StringComparison.Ordinal) && w.Length > key.Length + 1) return w.Substring(key.Length + 1);
            return null;
        }

        public static bool Has(string word) => All.Contains(word);

        public static int Int(string key, int fallback) => int.TryParse(Word(key), NumberStyles.Integer, CultureInfo.InvariantCulture, out int v) ? v : fallback;

        public static void Load(string modulePath)
        {
            try
            {
                string f = Path.Combine(modulePath, "autostart.txt");
                if (Environment.GetEnvironmentVariable("BANNERLORD_AUTOTEST") != "1" || !File.Exists(f)) return;
                Words = File.ReadAllText(f).Trim(); On = Words.Length > 0;
                Log.Write("autotest: " + Words);
            }
            catch (Exception e) { Log.Write("autotest load failed: " + e.Message); }
        }

        public static void Tick(float dt)
        {
            if (Deferred != null) { Action a = Deferred; Deferred = null; try { a(); } catch (Exception e) { Log.Write("deferred failed: " + e); } }
            if (!On) return;
            try
            {
                _stageT += dt;
                ScreenBase top = ScreenManager.TopScreen;
                string name = top == null ? "null" : top.GetType().Name;
                if (name != _lastScreen) { Log.Write("screen -> " + name); _lastScreen = name; }
                if (GameStateManager.Current?.ActiveState is VideoPlaybackState vs) { vs.OnVideoFinished(); Log.Write("video skipped"); }
                if (_stage == 0 && name.Contains("Initial") && _stageT > 4f + Int("menu", 0))
                {
                    _stage = 1; _stageT = 0;
                    Log.Write("starting custom game");
                    MBGameManager.StartNewGame(new CustomGameManager());
                }
                else if (_stage == 1 && name == "CustomBattleScreen" && _stageT > 3f)
                {
                    _stage = 2; _stageT = 0;
                    StartBattle(top);
                }
                else if (_stage == 1 && _stageT > 120f) Fail("the custom battle screen never came");
            }
            catch (Exception e) { Fail("tick failed: " + e); }
        }

        // The Custom Battle screen's view model is driven by reflection: its types are internal to the game's UI module.
        static bool Select(object group, string selector, Func<object, bool> match)
        {
            object sel = group.GetType().GetProperty(selector).GetValue(group);
            int i = 0;
            foreach (object it in (System.Collections.IEnumerable)sel.GetType().GetProperty("ItemList").GetValue(sel))
            {
                if (match(it)) { sel.GetType().GetProperty("SelectedIndex").SetValue(sel, i); return true; }
                i++;
            }
            return false;
        }

        static void StartBattle(ScreenBase screen)
        {
            object vm = screen.GetType().GetField("_dataSource", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(screen);
            string gtype = Word("type");
            if (gtype != null)
            {
                object gg = vm.GetType().GetProperty("GameTypeSelectionGroup").GetValue(vm);
                bool ok = Select(gg, "GameTypeSelection", it =>
                {
                    PropertyInfo pi = it.GetType().GetProperty("GameTypeStringId");
                    return pi != null && string.Equals((string)pi.GetValue(it), gtype, StringComparison.OrdinalIgnoreCase);
                });
                Log.Write("game type " + gtype + (ok ? " selected" : " NOT OFFERED"));
                if (!ok) { Fail("game type not offered"); return; }
            }
            object maps = vm.GetType().GetProperty("MapSelectionGroup").GetValue(vm);
            string want = Word("scene");
            if (want != null)
            {
                bool found = Select(maps, "MapSelection", it => (string)it.GetType().GetProperty("MapId").GetValue(it) == want);
                Log.Write("custom battle map " + want + (found ? " selected" : " NOT IN THE MAP LIST"));
                if (!found) { Fail("scene not listed"); return; }
            }
            int tod = Int("tod", -1);
            if (tod >= 0)
            {
                object ts = maps.GetType().GetProperty("TimeOfDaySelection").GetValue(maps);
                int best = -1, bestD = 99, i = 0;
                foreach (object it in (System.Collections.IEnumerable)ts.GetType().GetProperty("ItemList").GetValue(ts))
                {
                    int t = Convert.ToInt32(it.GetType().GetProperty("TimeOfDay").GetValue(it));
                    if (Math.Abs(t - tod) < bestD) { bestD = Math.Abs(t - tod); best = i; }
                    i++;
                }
                if (best >= 0) ts.GetType().GetProperty("SelectedIndex").SetValue(ts, best);
                Log.Write("time of day: entry " + best);
            }
            string season = Word("season");
            if (season != null)
            {
                bool hit = Select(maps, "SeasonSelection", it =>
                {
                    foreach (string prop in new[] { "SeasonId", "StringItem" })
                    {
                        PropertyInfo pi = it.GetType().GetProperty(prop);
                        if (pi != null && pi.GetValue(it) != null && pi.GetValue(it).ToString().ToLowerInvariant().Contains(season.ToLowerInvariant())) return true;
                    }
                    return false;
                });
                Log.Write("season " + season + (hit ? " selected" : " NOT OFFERED"));
            }
            Log.Write("ExecuteStart");
            vm.GetType().GetMethod("ExecuteStart").Invoke(vm, null);
        }

        public static void Fail(string why)
        {
            Log.Write("autotest FAILED: " + why);
            Done();
        }

        public static void Done()
        {
            On = false;
            Log.Write("AUTOTEST_DONE");
            if (Has("quit")) Utilities.QuitGame();
        }
    }

    // inside the battle: deployment, the charge, screenshots, the count, the end
    public class AutoMissionLogic : MissionLogic
    {
        static readonly float[] ShotTimes = { 3f, 8f, 14f, 22f, 32f, 45f, 58f };
        float _t, _fightT = -1f, _nextCount, _viewT;
        List<string[]> _views; int _view = -1; bool _viewShot; float _bearing, _elevation;
        bool _deployDone, _charged, _ended;
        int _shot, _spawnedUs, _spawnedThem;
        string _shotDir;

        // not in OnBehaviorInitialize: a behaviour added from OnMissionBehaviorInitialize does not always get that call
        void Setup()
        {
            string home = Environment.GetEnvironmentVariable("BANNERLORD_SDK_HOME");
            if (string.IsNullOrEmpty(home)) home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "bannerlord-sdk");
            _shotDir = Path.Combine(home, "out", "__MODLOWER___shots");
            try { Directory.CreateDirectory(_shotDir); } catch { }
            Log.Write("mission on scene " + Mission.SceneName);
            if (!AutoTest.Has("views")) return;
            string f = Path.Combine(SubModule.ModulePath, "SceneObj", Mission.SceneName, "views.txt");
            _views = new List<string[]>();
            if (File.Exists(f))
                foreach (string line in File.ReadAllLines(f))
                {
                    string[] w = line.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
                    if (w.Length == 7) _views.Add(w);
                }
            Log.Write("views: " + _views.Count + " from " + f);
        }

        static float F(string s) => float.Parse(s, CultureInfo.InvariantCulture);

        // True while the viewpoints are being shown: each one for 3 s, its screenshot after 2 s. The player is moved to the
        // viewpoint and the normal camera is turned towards the target: with a free camera the terrain stays culled for
        // the player's own position, so most of it would be missing from the picture.
        bool TickViews(float dt)
        {
            if (_views == null || _view >= _views.Count) return false;
            var ms = ScreenManager.TopScreen as MissionScreen;
            Agent main = Mission.MainAgent;
            if (ms == null || main == null || !main.IsActive()) return true;
            _viewT += dt;
            if (_view < 0 || _viewT > 3f)
            {
                _view++; _viewT = 0f; _viewShot = false;
                if (_view >= _views.Count) { Log.Write("views done"); return false; }
                string[] w = _views[_view];
                var eye = new Vec3(F(w[1]), F(w[2]), F(w[3])); var tgt = new Vec3(F(w[4]), F(w[5]), F(w[6]));
                Vec3 pos = eye; pos.z = Mission.Scene.GetGroundHeightAtPosition(new Vec3(eye.x, eye.y, eye.z + 50f));
                (main.MountAgent ?? main).TeleportToPosition(pos);
                Vec3 look = tgt - pos; float flat = (float)Math.Sqrt(look.x * look.x + look.y * look.y);
                _bearing = (float)Math.Atan2(-look.x, look.y);                 // the camera's bearing: 0 looks along +y
                _elevation = flat > 1f ? (float)Math.Atan2(look.z - 2f, flat) : -0.2f;
                var dir = new Vec2(look.x, look.y); if (dir.Length > 0.01f) { dir.Normalize(); main.SetMovementDirection(in dir); }
            }
            ms.CameraBearing = _bearing;
            try { typeof(MissionScreen).GetProperty("CameraElevation").SetValue(ms, _elevation); } catch { }
            if (!_viewShot && _viewT > 2f)
            {
                _viewShot = true;
                string f = Path.Combine(_shotDir, Mission.SceneName + "_view" + _view + "_" + _views[_view][0] + ".bmp");
                Utilities.TakeScreenshot(f);
                Log.Write("view " + _views[_view][0] + " -> " + f);
            }
            return true;
        }

        public override void OnAgentBuild(Agent agent, Banner banner)
        {
            if (!agent.IsHuman || agent.Team == null) return;
            if (agent.Team == Mission.PlayerTeam || agent.Team.IsPlayerAlly) _spawnedUs++; else _spawnedThem++;
        }

        int Alive(bool us)
        {
            int n = 0;
            foreach (Agent a in Mission.Agents)
                if (a.IsHuman && a.IsActive() && a.Team != null && ((a.Team == Mission.PlayerTeam || a.Team.IsPlayerAlly) == us)) n++;
            return n;
        }

        public override void OnMissionTick(float dt)
        {
            if (_ended) return;
            _t += dt;
            try
            {
                if (_shotDir == null) Setup();
                if (InformationManager.IsAnyInquiryActive()) { Log.Write("an inquiry popup is open: closing it"); InformationManager.HideInquiry(); }
                if (!_deployDone && _t > 5f)
                {
                    DeploymentHandler dh = Mission.GetMissionBehavior<DeploymentHandler>();
                    DeploymentMissionController dmc = Mission.GetMissionBehavior<DeploymentMissionController>();
                    if (dh == null || dmc == null) { _deployDone = true; Log.Write("no deployment phase"); }
                    else if (dmc.TeamSetupOver) { Log.Write("finishing deployment"); AutoTest.Deferred = dh.FinishDeployment; _deployDone = true; }
                }
                Agent main = Mission.MainAgent;
                if (_deployDone && _fightT < 0f && _t > 8f && main != null)
                {
                    _fightT = _t;
                    Vec3 p = main.Position;
                    Log.Write("spawned " + _spawnedUs + " + " + _spawnedThem + "; player at " + p.x.ToString("F1") + ", " + p.y.ToString("F1") + ", " + p.z.ToString("F1")
                              + ", ground under the player " + Mission.Scene.GetGroundHeightAtPosition(p).ToString("F2")
                              + ", navmesh faces " + Mission.Scene.GetNavMeshFaceCount());
                }
                if (_fightT < 0f) return;
                if (TickViews(dt)) { _fightT = _t; return; }      // the battle clock starts after the viewpoints
                float ft = _t - _fightT;
                if (AutoTest.Has("fight") && !_charged && ft > 4f && Mission.PlayerTeam != null)
                {
                    _charged = true;
                    OrderController oc = Mission.PlayerTeam.PlayerOrderController;
                    if (oc != null) { oc.SelectAllFormations(); oc.SetOrder(OrderType.Charge); }
                    if (main != null && main.IsActive()) main.Controller = AgentControllerType.AI;
                    Log.Write("charge ordered");
                }
                if (_shot < ShotTimes.Length && ft > ShotTimes[_shot])
                {
                    string f = Path.Combine(_shotDir, Mission.SceneName + "_" + _shot + ".bmp");
                    Utilities.TakeScreenshot(f);
                    Log.Write("screenshot " + f);
                    _shot++;
                }
                if (ft > _nextCount)
                {
                    _nextCount += 10f;
                    Log.Write("t " + ft.ToString("F0") + " s: alive " + Alive(true) + " vs " + Alive(false));
                }
                int secs = AutoTest.Int("secs", 60);
                bool over = Alive(true) == 0 || Alive(false) == 0;
                if (ft > secs || (over && ft > 12f))
                {
                    _ended = true;
                    Log.Write("battle test over after " + ft.ToString("F0") + " s: alive " + Alive(true) + " vs " + Alive(false) + " of " + _spawnedUs + " vs " + _spawnedThem
                              + (over ? " (one side is gone)" : ""));
                    AutoTest.Done();
                }
            }
            catch (Exception e) { _ended = true; AutoTest.Fail("mission tick failed: " + e); }
        }
    }
}
