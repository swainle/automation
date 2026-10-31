import test from "node:test";
import assert from "node:assert/strict";
import {allowedYidunPage, easeOutBounce, dragYidun} from "../../extension/yidun.mjs";

test("drag restricted to exact demo or local fixture", () => {
  assert.equal(allowedYidunPage("https://dun.163.com/trial/jigsaw"), true);
  assert.equal(allowedYidunPage("http://127.0.0.1:1234/extension-test/yidun/index.html"), true);
  for (const url of ["https://mms.pinduoduo.com/login", "https://dun.163.com.evil.test/trial/jigsaw", "https://dun.163.com/", "http://dun.163.com/trial/jigsaw"]) assert.equal(allowedYidunPage(url), false);
});
test("bounce endpoints and trajectory stay bounded", () => {
  assert.equal(easeOutBounce(0), 0);
  assert.equal(easeOutBounce(1), 1);
  for (let i=0;i<=100;i++) assert.ok(easeOutBounce(i/100)>=0 && easeOutBounce(i/100)<=1);
});
function apiFixture(failMove=false) {
  const commands=[];
  const probe={fingerprint:"fixture",status:"pending",geometry:{button:{x:10,y:10,width:40,height:40},track:{x:10,y:10,width:320,height:40}}};
  return {commands, probe,
    tabs:{get:async()=>({url:"https://dun.163.com/trial/jigsaw"})},
    scripting:{executeScript:async()=>[{result:{ok:true,result:probe}}]},
    debugger:{attach:async()=>commands.push("attach"),detach:async()=>commands.push("detach"),
      sendCommand:async(t,method,data)=>{commands.push(data.type);if(failMove && data.type==="mouseMoved" && data.buttons===1) throw new Error("movement failed");}}
  };
}
test("stale challenge refuses mouse input and detaches", async () => {
  const api=apiFixture();
  await assert.rejects(dragYidun(api,1,{distance:100,fingerprint:"stale"}),/yidun_challenge_changed/);
  assert.deepEqual(api.commands,["attach","detach"]);
});
test("movement failure releases mouse and detaches without retry", async () => {
  const api=apiFixture(true);
  await assert.rejects(dragYidun(api,1,{distance:100,fingerprint:"fixture"}));
  assert.deepEqual(api.commands.slice(-2),["mouseReleased","detach"]);
  assert.equal(api.commands.filter(x=>x==="mousePressed").length,1);
});
