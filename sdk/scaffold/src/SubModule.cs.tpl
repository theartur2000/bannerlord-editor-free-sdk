// __NAME__: the module's entry point.
//
// The game calls the overrides below. What is here already: a log file (Modules/__MOD__/__MODLOWER___log.txt), the
// unattended test driver (AutoTest.cs), the scratch scene slots (SceneSlots.cs) and the bridge that takes commands from
// outside while the game runs (Bridge.cs). Put the mod's own code in new files and hook it in where the comments say.
using System;
using System.IO;
using TaleWorlds.Core;
using TaleWorlds.ModuleManager;
using TaleWorlds.MountAndBlade;

namespace __MOD__
{
    public class SubModule : MBSubModuleBase
    {
        public const string ModuleId = "__MOD__";
        public static string ModulePath => ModuleHelper.GetModuleFullPath(ModuleId);

        protected override void OnSubModuleLoad()
        {
            base.OnSubModuleLoad();
            Log.Write("__MOD__ loaded");
            SceneSlots.EmptyUsed();
            AutoTest.Load(ModulePath);
        }

        protected override void OnApplicationTick(float dt)
        {
            base.OnApplicationTick(dt);
            AutoTest.Tick(dt);
            Bridge.Tick(dt);
        }

        public override void OnMissionBehaviorInitialize(Mission mission)
        {
            base.OnMissionBehaviorInitialize(mission);
            if (AutoTest.On) mission.AddMissionBehavior(new AutoMissionLogic());
            // the mod's own mission behaviours go here
        }

        protected override void OnGameStart(Game game, IGameStarter gameStarter)
        {
            base.OnGameStart(game, gameStarter);
            // campaign models and behaviours go here: if (gameStarter is CampaignGameStarter starter) { ... }
        }
    }

    // One line per call, with the time, flushed at once: the harness reads this file while the game runs.
    static class Log
    {
        static readonly object Gate = new object();

        public static void Write(string line)
        {
            try
            {
                lock (Gate) File.AppendAllText(Path.Combine(SubModule.ModulePath, "__MODLOWER___log.txt"), DateTime.Now.ToString("HH:mm:ss.fff") + " " + line + Environment.NewLine);
            }
            catch { }
        }
    }
}
