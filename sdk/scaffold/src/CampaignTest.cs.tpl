// The campaign half of the unattended test driver (test word "campaign"): a new sandbox campaign, character creation
// clicked through, and then on the map:
//   near=<settlement id>   move the party next to that settlement
//   battle                 give the party troops (men=<n>, default 60), put a party of looters (foes=<n>) next to it and attack:
//                          the battle runs on whatever scene the campaign picks
// Without "battle" the test stays on the campaign map, for checks through the bridge (`bl live settlement`).
// Needs the Sandbox module in the module list (`bl run --campaign`). Nothing is saved.
using System;
using System.Linq;
using Helpers;
using TaleWorlds.CampaignSystem;
using TaleWorlds.CampaignSystem.CharacterCreationContent;
using TaleWorlds.CampaignSystem.Encounters;
using TaleWorlds.CampaignSystem.GameState;
using TaleWorlds.CampaignSystem.Party;
using TaleWorlds.CampaignSystem.Party.PartyComponents;
using TaleWorlds.CampaignSystem.Roster;
using TaleWorlds.CampaignSystem.Settlements;
using TaleWorlds.Core;
using TaleWorlds.Library;
using TaleWorlds.Localization;
using TaleWorlds.MountAndBlade;
using TaleWorlds.ObjectSystem;

namespace __MOD__
{
    static class CampaignTest
    {
        static int _stage; static float _t;

        public static void Tick(float dt, string screenName)
        {
            _t += dt;
            GameState state = GameStateManager.Current?.ActiveState;
            switch (_stage)
            {
                case 0:
                    if (screenName.Contains("Initial") && _t > 4f) { _stage = 1; _t = 0; Log.Write("campaign test: starting a sandbox campaign"); Start(); }
                    break;
                case 1:
                    if (state is CharacterCreationState cc && _t > 3f)
                    {
                        var mgr = cc.CharacterCreationManager; var content = mgr.CharacterCreationContent;
                        if (content.SelectedCulture == null) content.SetSelectedCulture(content.GetCultures().First(), mgr);
                        _t = 2.5f;
                        int before = mgr.GetIndexOfCurrentStage(); mgr.NextStage();
                        Log.Write("campaign test: character creation stage " + before + " -> " + mgr.GetIndexOfCurrentStage());
                    }
                    else if (state is MapState && _t > 1f) { _stage = 2; _t = 0; Log.Write("campaign test: on the map"); }
                    else if (_t > 240f) AutoTest.Fail("campaign test: stuck in " + state?.GetType().Name);
                    break;
                case 2:
                    if (_t > 6f) { _stage = 3; _t = 0; Setup(); if (!AutoTest.Has("battle")) { _stage = 99; Log.Write("campaign test: staying on the map"); } }
                    break;
                case 3:
                    if (_t > 3f) { _stage = 4; _t = 0; EncounterManager.StartPartyEncounter(PartyBase.MainParty, _enemy.Party); Log.Write("campaign test: encounter started"); }
                    break;
                case 4:
                    if (_t > 3f)
                    {
                        if (Campaign.Current.ConversationManager.IsConversationInProgress) Campaign.Current.ConversationManager.EndConversation();
                        _stage = 5; _t = 0;
                    }
                    break;
                case 5:
                    if (_t > 3f)
                    {
                        Log.Write("campaign test: menu " + Campaign.Current.CurrentMenuContext?.GameMenu?.StringId + ", attacking");
                        _stage = 6; _t = 0;
                        MenuHelper.EncounterAttackConsequence(null);
                    }
                    break;
                case 6:
                    if (Mission.Current != null) { _stage = 7; Log.Write("campaign test: mission opened on " + Mission.Current.SceneName); }
                    else if (_t > 90f) AutoTest.Fail("campaign test: no mission after the attack");
                    break;
            }
        }

        static MobileParty _enemy;

        static void Start() => MBGameManager.StartNewGame(new SandBox.SandBoxGameManager(() => new Campaign(CampaignGameMode.Campaign)));

        // a spot about `dist` map units from `from` that lies on a land face; `from` itself when none of eight directions does
        static CampaignVec2 Spot(CampaignVec2 from, float dist)
        {
            for (int k = 0; k < 8; k++)
            {
                var v = from + new CampaignVec2(new Vec2(dist * (float)Math.Cos(k * Math.PI / 4), dist * (float)Math.Sin(k * Math.PI / 4)), true);
                try
                {
                    if (v.Face.IsValid() && Campaign.Current.Models.PartyNavigationModel.IsTerrainTypeValidForNavigationType(
                            Campaign.Current.MapSceneWrapper.GetFaceTerrainType(v.Face), MobileParty.NavigationType.Default)) return v;
                }
                catch { }
            }
            return from;
        }

        static void Setup()
        {
            MobileParty main = MobileParty.MainParty;
            string near = AutoTest.Word("near");
            if (near != null)
            {
                Settlement s = Settlement.Find(near);
                if (s == null) Log.Write("campaign test: no settlement " + near);
                else { main.Position = Spot(s.GatePosition, 2.5f); Log.Write("campaign test: party moved next to " + s.Name + " at " + main.Position.ToVec2()); }
            }
            if (!AutoTest.Has("battle")) return;
            CharacterObject recruit = MBObjectManager.Instance.GetObject<CharacterObject>("imperial_recruit");
            CharacterObject looter = MBObjectManager.Instance.GetObject<CharacterObject>("looter");
            main.MemberRoster.AddToCounts(recruit, AutoTest.Int("men", 60));
            TroopRoster roster = TroopRoster.CreateDummyTroopRoster(); roster.AddToCounts(looter, AutoTest.Int("foes", 60));
            Clan clan = Clan.BanditFactions.FirstOrDefault(c => c.StringId == "looters") ?? Clan.BanditFactions.First();
            Settlement home = Settlement.All.OrderBy(x => x.GatePosition.ToVec2().DistanceSquared(main.Position.ToVec2())).First();
            CampaignVec2 spot = Spot(main.Position, 1.3f);
            _enemy = CustomPartyComponent.CreateCustomPartyWithTroopRoster(spot, 0f, home, new TextObject("Test Looters"), clan, roster, TroopRoster.CreateDummyTroopRoster(), null, "", "", 0.5f);
            _enemy.Ai.SetDoNotMakeNewDecisions(true);
            _enemy.SetMoveGoToPoint(_enemy.Position, MobileParty.NavigationType.Default);
            Log.Write("campaign test: party of " + main.MemberRoster.TotalManCount + " at " + main.Position.ToVec2() + ", " + roster.TotalManCount + " looters at " + _enemy.Position.ToVec2()
                      + ", season " + CampaignTime.Now.GetSeasonOfYear + ", hour " + CampaignTime.Now.CurrentHourInDay.ToString("F0"));
        }
    }
}
