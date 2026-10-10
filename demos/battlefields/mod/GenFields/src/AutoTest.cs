// Unattended test driver of the GenFields demo, for the SDK's harness (harness/night_test.ps1).
//
// When the environment variable BANNERLORD_AUTOTEST is 1 and Modules/GenFields/autostart.txt exists, the words in that file
// are run and the game quits by itself:
//   scene=<id>   the Custom Battle map to fight on (a scene listed in ModuleData/genfields_custom_battle_scenes.xml)
//   late=<id>    copy that scene from the SDK's stage folder into the module at the main menu, after the game has started
//                (the test of loading a scene folder that did not exist at startup; install it with install.py --late)
//   swap=<id>    at the main menu, overwrite the files of the scene named by scene= (installed before the game started)
//                with the staged scene <id>: the test of a pool of scene folders rewritten before a battle
//   tod=<hour>   time of day of the battle (the nearest one Custom Battle offers)
//   fight        finish deployment, order the player's side to charge, let the AI play the player
//   secs=<n>     seconds of battle before quitting (default 60)
//   quit         quit the game at the end (without it the battle stays open)
// It writes screenshots to <SDK home>\out\genfields_shots and one line every 10 s with the troops alive on both sides.
// The last line is AUTOTEST_DONE, the marker the harness waits for.
using System;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using TaleWorlds.Core;
using TaleWorlds.Engine;
using TaleWorlds.Library;
using TaleWorlds.MountAndBlade;
using TaleWorlds.MountAndBlade.CustomBattle;
using TaleWorlds.ScreenSystem;
using Path = System.IO.Path;

namespace GenFields
{
    static class AutoTest
    {
        public static bool On;
        public static string Words = "";
        public static Action Deferred;
        static float _stageT;
        static int _stage;
        static string _lastScreen;

        public static string Word(string key)
        {
            foreach (string w in Words.Split(new[] { ' ', '\t', '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries))
                if (w.StartsWith(key + "=", StringComparison.Ordinal) && w.Length > key.Length + 1) return w.Substring(key.Length + 1);
            return null;
        }

        public static bool Has(string word) => Words.Split(new[] { ' ', '\t', '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries).Contains(word);

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
                if (_stage == 0 && name.Contains("Initial") && _stageT > 4f)
                {
                    _stage = 1; _stageT = 0;
                    string late = Word("late");
                    if (late != null)
                    {
                        bool had = File.Exists(Path.Combine(ModuleHelperPath(), "SceneObj", late, "scene.xscene"));
                        Log.Write("late scene " + late + (had ? " WAS ALREADY INSTALLED (the test proves nothing)" : " is not in the module yet") + "; game running for " + _stageT.ToString("F0") + " s at the menu");
                        if (!Control.EnsureScene(late)) { Fail("late scene could not be copied"); return; }
                    }
                    string swap = Word("swap");
                    if (swap != null && !Control.Overwrite(swap, Word("scene"))) { Fail("swap failed"); return; }
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

        static void StartBattle(ScreenBase screen)
        {
            object vm = screen.GetType().GetField("_dataSource", BindingFlags.NonPublic | BindingFlags.Instance).GetValue(screen);
            string want = Word("scene");
            if (want != null)
            {
                object mg = vm.GetType().GetProperty("MapSelectionGroup").GetValue(vm);
                object ms = mg.GetType().GetProperty("MapSelection").GetValue(mg);
                int i = 0; bool found = false;
                foreach (object it in (System.Collections.IEnumerable)ms.GetType().GetProperty("ItemList").GetValue(ms))
                {
                    if ((string)it.GetType().GetProperty("MapId").GetValue(it) == want) { ms.GetType().GetProperty("SelectedIndex").SetValue(ms, i); found = true; break; }
                    i++;
                }
                Log.Write("custom battle map " + want + (found ? " selected" : " NOT IN THE MAP LIST"));
                if (!found) { Fail("scene not listed"); return; }
            }
            int tod = Int("tod", -1);
            if (tod >= 0)
            {
                object mg = vm.GetType().GetProperty("MapSelectionGroup").GetValue(vm);
                object ts = mg.GetType().GetProperty("TimeOfDaySelection").GetValue(mg);
                int i = 0, best = -1, bestD = 99, bestT = -1;
                foreach (object it in (System.Collections.IEnumerable)ts.GetType().GetProperty("ItemList").GetValue(ts))
                {
                    int t = Convert.ToInt32(it.GetType().GetProperty("TimeOfDay").GetValue(it));
                    if (Math.Abs(t - tod) < bestD) { bestD = Math.Abs(t - tod); best = i; bestT = t; }
                    i++;
                }
                if (best >= 0) ts.GetType().GetProperty("SelectedIndex").SetValue(ts, best);
                Log.Write("time of day " + bestT);
            }
            Log.Write("ExecuteStart");
            vm.GetType().GetMethod("ExecuteStart").Invoke(vm, null);
        }

        static string ModuleHelperPath() => TaleWorlds.ModuleManager.ModuleHelper.GetModuleFullPath(SubModule.ModuleId);

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
        float _t, _fightT = -1f, _nextCount;
        bool _deployDone, _charged, _ended;
        int _shot, _spawnedUs, _spawnedThem;
        string _shotDir;

        // not in OnBehaviorInitialize: a behaviour added from OnMissionBehaviorInitialize did not get that call in the first run
        void Setup()
        {
            string home = Environment.GetEnvironmentVariable("BANNERLORD_SDK_HOME");
            if (string.IsNullOrEmpty(home)) home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "bannerlord-sdk");
            _shotDir = Path.Combine(home, "out", "genfields_shots");
            try { Directory.CreateDirectory(_shotDir); } catch { }
            Log.Write("mission on scene " + Mission.SceneName);
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
